import torch
import torch.nn as nn
from torch.nn import functional as nnf
from torch.utils.data import Dataset, DataLoader
from enum import Enum
from transformers import GPT2Tokenizer, GPT2LMHeadModel, AdamW, get_linear_schedule_with_warmup
from tqdm import tqdm
import os
import pickle
import sys
import argparse
import json
import numpy as np
from typing import Tuple, Optional, Union
# import matplotlib.pyplot as plt
import copy
# from evaluation.bleu.bleu import Bleu
# from evaluation.rouge.rouge import Rouge
# from evaluation.cider.cider import Cider
# from evaluation.meteor.meteor import Meteor
# from evaluation.tokenizer.ptbtokenizer import PTBTokenizer
import time

class MappingType(Enum):
    MLP = 'mlp'
    Transformer = 'transformer'


class ClipCocoDataset(Dataset):

    def __len__(self) -> int:
        return len(self.captions_tokens)

    def pad_tokens(self, item: int):
        tokens = self.captions_tokens[item]
        padding = self.max_seq_len - tokens.shape[0]
        if padding > 0:
            tokens = torch.cat((tokens, torch.zeros(padding, dtype=torch.int64) - 1))
            self.captions_tokens[item] = tokens
        elif padding < 0:
            tokens = tokens[:self.max_seq_len]
            self.captions_tokens[item] = tokens
        mask = tokens.ge(0)  # mask is zero where we out of sequence
        tokens[~mask] = 0
        mask = mask.float()
        # SOS
        mask = torch.cat((torch.ones(self.prefix_length), mask), dim=0)  # adding prefix mask
        return tokens, mask

    def __getitem__(self, item: int) -> Tuple[torch.Tensor, ...]:
        tokens, mask = self.pad_tokens(item)
        prefix = self.prefixes[self.caption2embedding[item]]
        if self.normalize_prefix:
            prefix = prefix.float()
            prefix = prefix / prefix.norm(2, -1)

        # tokenized caption, mask attention , (prefix --> actual image)
        return tokens, mask, prefix

    def __init__(self, data_path: str, prefix_length: int, gpt2_type: str = "gpt2",
                 normalize_prefix=False):
        self.tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
        self.prefix_length = prefix_length
        self.normalize_prefix = normalize_prefix
        with open(data_path, 'rb') as f:
            all_data = pickle.load(f)
        print("Data size is %0d" % len(all_data["clip_embedding"]))
        sys.stdout.flush()
        self.prefixes = all_data["clip_embedding"]
        captions_raw = all_data["captions"]
        # image ids kai captions
        self.image_ids = [caption["image_id"] for caption in captions_raw]
        self.captions = [caption['caption'] for caption in captions_raw]
        self.captions_tokens = []
        self.caption2embedding = []
        max_seq_len = 0
        for caption in captions_raw:
            # tokenize to caption
            self.captions_tokens.append(torch.tensor(self.tokenizer.encode(caption['caption']), dtype=torch.int64))
            # clip_embedding einai to sequential ID !!
            self.caption2embedding.append(caption["clip_embedding"])
            max_seq_len = max(max_seq_len, self.captions_tokens[-1].shape[0])

        all_len = torch.tensor([len(self.captions_tokens[i]) for i in range(len(self))]).float()
        self.max_seq_len = min(int(all_len.mean() + all_len.std() * 10), int(all_len.max()))
        print('max_seq_len of tokens :  ' + str(self.max_seq_len))


class MLP(nn.Module):

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model(x)

    def __init__(self, sizes: Tuple[int, ...], bias=True, act=nn.Tanh):
        super(MLP, self).__init__()
        layers = []
        for i in range(len(sizes) - 1):
            layers.append(nn.Linear(sizes[i], sizes[i + 1], bias=bias))
            if i < len(sizes) - 2:
                layers.append(act())
        self.model = nn.Sequential(*layers)


class MlpTransformer(nn.Module):
    def __init__(self, in_dim, h_dim, out_d: Optional[int] = None, act=nnf.relu, dropout=0.):
        super().__init__()
        out_d = out_d if out_d is not None else in_dim
        self.fc1 = nn.Linear(in_dim, h_dim)
        self.act = act
        self.fc2 = nn.Linear(h_dim, out_d)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.dropout(x)
        x = self.fc2(x)
        x = self.dropout(x)
        return x


class MultiHeadAttention(nn.Module):

    def __init__(self, dim_self, dim_ref, num_heads, bias=True, dropout=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim_self // num_heads
        self.scale = head_dim ** -0.5
        self.to_queries = nn.Linear(dim_self, dim_self, bias=bias)
        self.to_keys_values = nn.Linear(dim_ref, dim_self * 2, bias=bias)
        self.project = nn.Linear(dim_self, dim_self)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, y=None, mask=None):
        y = y if y is not None else x
        b, n, c = x.shape
        _, m, d = y.shape
        # b n h dh
        queries = self.to_queries(x).reshape(b, n, self.num_heads, c // self.num_heads)
        # b m 2 h dh
        keys_values = self.to_keys_values(y).reshape(b, m, 2, self.num_heads, c // self.num_heads)
        keys, values = keys_values[:, :, 0], keys_values[:, :, 1]
        attention = torch.einsum('bnhd,bmhd->bnmh', queries, keys) * self.scale
        if mask is not None:
            if mask.dim() == 2:
                mask = mask.unsqueeze(1)
            attention = attention.masked_fill(mask.unsqueeze(3), float("-inf"))
        attention = attention.softmax(dim=2)
        out = torch.einsum('bnmh,bmhd->bnhd', attention, values).reshape(b, n, c)
        out = self.project(out)
        return out, attention


class TransformerLayer(nn.Module):

    def forward_with_attention(self, x, y=None, mask=None):
        x_, attention = self.attn(self.norm1(x), y, mask)
        x = x + x_
        x = x + self.mlp(self.norm2(x))
        return x, attention

    def forward(self, x, y=None, mask=None):
        x = x + self.attn(self.norm1(x), y, mask)[0]
        x = x + self.mlp(self.norm2(x))
        return x

    def __init__(self, dim_self, dim_ref, num_heads, mlp_ratio=4., bias=False, dropout=0., act=nnf.relu,
                 norm_layer: nn.Module = nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim_self)
        self.attn = MultiHeadAttention(dim_self, dim_ref, num_heads, bias=bias, dropout=dropout)
        self.norm2 = norm_layer(dim_self)
        self.mlp = MlpTransformer(dim_self, int(dim_self * mlp_ratio), act=act, dropout=dropout)


class Transformer(nn.Module):

    def forward_with_attention(self, x, y=None, mask=None):
        attentions = []
        for layer in self.layers:
            x, att = layer.forward_with_attention(x, y, mask)
            attentions.append(att)
        return x, attentions

    def forward(self, x, y=None, mask=None):
        for i, layer in enumerate(self.layers):
            if i % 2 == 0 and self.enc_dec:  # cross
                x = layer(x, y)
            elif self.enc_dec:  # self
                x = layer(x, x, mask)
            else:  # self or cross
                x = layer(x, y, mask)
        return x

    def __init__(self, dim_self: int, num_heads: int, num_layers: int, dim_ref: Optional[int] = None,
                 mlp_ratio: float = 2., act=nnf.relu, norm_layer: nn.Module = nn.LayerNorm, enc_dec: bool = False):
        super(Transformer, self).__init__()
        print('*** Initiate Transformer with {} Layers *** '.format(num_layers))
        dim_ref = dim_ref if dim_ref is not None else dim_self
        self.enc_dec = enc_dec
        if enc_dec:
            num_layers = num_layers * 2
        layers = []
        for i in range(num_layers):
            if i % 2 == 0 and enc_dec:  # cross
                layers.append(TransformerLayer(dim_self, dim_ref, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
            elif enc_dec:  # self
                layers.append(
                    TransformerLayer(dim_self, dim_self, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
            else:  # self or cross
                # dim self 768
                layers.append(TransformerLayer(dim_self, dim_ref, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
        self.layers = nn.ModuleList(layers)


class TransformerMapper(nn.Module):

    def forward(self, x):
        # apo 1 x 512
        # 1 x 10 x 768 Both?
        x = self.linear(x).view(x.shape[0], self.clip_length, -1)
        prefix = self.prefix_const.unsqueeze(0).expand(x.shape[0], *self.prefix_const.shape)
        prefix = torch.cat((x, prefix), dim=1)
        # dekati stili kai meta,
        # result 1x10x768   --  original output 1x20x768
        out = self.transformer(prefix)[:, self.clip_length:]
        return out

    def __init__(self, dim_clip: int, dim_embedding: int, prefix_length: int, clip_length: int, num_layers: int = 8):
        super(TransformerMapper, self).__init__()
        print('*** Initiate TransformerMapper *** ')
        self.clip_length = clip_length

        self.transformer = Transformer(dim_embedding, 8, num_layers)
        self.linear = nn.Linear(dim_clip, clip_length * dim_embedding)
        # 10 x 768
        self.prefix_const = nn.Parameter(torch.randn(prefix_length, dim_embedding), requires_grad=True)


class ClipCaptionModel(nn.Module):

    def get_dummy_token(self, batch_size: int, device: torch.device) -> torch.Tensor:
        return torch.zeros(batch_size, self.prefix_length, dtype=torch.int64, device=device)

    def forward(self, tokens: torch.Tensor, prefix: torch.Tensor, mask: Optional[torch.Tensor] = None,
                labels: Optional[torch.Tensor] = None):

        embedding_text = self.gpt.transformer.wte(tokens)
        prefix_projections = self.clip_project(prefix).view(-1, self.prefix_length, self.gpt_embedding_size)
        embedding_cat = torch.cat((prefix_projections, embedding_text), dim=1)
        out = self.gpt(inputs_embeds=embedding_cat, labels=None, attention_mask=mask)
        return out

    def __init__(self, prefix_length: int, clip_length: Optional[int] = None, prefix_size: int = 512,
                 num_layers: int = 8, mapping_type: MappingType = MappingType.MLP):
        super(ClipCaptionModel, self).__init__()
        print('*** Initiating the ClipCaptionModel *** ')
        self.prefix_length = prefix_length
        self.gpt = GPT2LMHeadModel.from_pretrained('gpt2')
        # 768
        self.gpt_embedding_size = self.gpt.transformer.wte.weight.shape[1]
        if mapping_type == MappingType.MLP:
            self.clip_project = MLP((prefix_size, (self.gpt_embedding_size * prefix_length) // 2,
                                     self.gpt_embedding_size * prefix_length))
        else:
            self.clip_project = TransformerMapper(prefix_size, self.gpt_embedding_size, prefix_length,
                                                  clip_length, num_layers)


class ClipCaptionPrefix(ClipCaptionModel):

    def parameters(self, recurse: bool = True):
        return self.clip_project.parameters()

    def train(self, mode: bool = True):
        super(ClipCaptionPrefix, self).train(mode)
        self.gpt.eval()
        return self


def save_config(args: argparse.Namespace):
    config = {}
    for key, item in args._get_kwargs():
        config[key] = item
    out_path = os.path.join(args.out_dir, f"{args.prefix}.json")
    with open(out_path, 'w') as outfile:
        json.dump(config, outfile)


def load_model(config_path: str, epoch_or_latest: Union[str, int] = '_latest'):
    with open(config_path) as f:
        config = json.load(f)
    parser = argparse.ArgumentParser()
    parser.set_defaults(**config)
    args = parser.parse_args()
    if type(epoch_or_latest) is int:
        epoch_or_latest = f"-{epoch_or_latest:03d}"
    model_path = os.path.join(args.out_dir, f"{args.prefix}{epoch_or_latest}.pt")
    if args.only_prefix:
        model = ClipCaptionPrefix(args.prefix_length)
    else:
        model = ClipCaptionModel(args.prefix_length)
    if os.path.isfile(model_path):
        print(f"loading model from {model_path}")
        model.load_state_dict(torch.load(model_path, map_location=torch.device('cpu')))
    else:
        print(f"{model_path} is not exist")
    return model, parser


def apply_validation(model, val_dataloader, epoch, prefix_length):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    val_loss = 0
    model.eval()
    for idx, (tokens, mask, prefix) in enumerate(val_dataloader):
        tokens, mask, prefix = tokens.to(device), mask.to(device), prefix.to(device, dtype=torch.float32)
        with torch.no_grad():
            outputs = model(tokens, prefix, mask)
            logits = outputs.logits[:, prefix_length - 1: -1]
            loss = nnf.cross_entropy(logits.reshape(-1, logits.shape[-1]), tokens.flatten(), ignore_index=0)
            val_loss = val_loss + loss.item()

    avg_val_loss = val_loss / len(val_dataloader)
    print('*** In Epoch {} the average validation loss : {} ***'.format(epoch, avg_val_loss))
    return avg_val_loss


class EarlyStopping:
    def __init__(self, tolerance=5, delta=0.5):

        self.tolerance = tolerance
        self.delta = delta
        self.counter = 0
        self.early_stop = False

    def __call__(self, train_loss, validation_loss):
        if (validation_loss - train_loss) > self.delta:
            self.counter += 1
            if self.counter >= self.tolerance:
                self.early_stop = True


def train(model: ClipCaptionModel, train_dataset: ClipCocoDataset,
          val_dataset: ClipCocoDataset, myconfig, lr: float = 2e-5,
          warmup_steps: int = 5000, output_dir: str = ".", model_name: str = ""):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # test_tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    model = model.to(device)
    optimizer = AdamW(model.parameters(), lr=lr)
    batch_size = myconfig.get('batch_size')
    epochs = myconfig.get('epochs')

    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=False, drop_last=False)
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, drop_last=False)

    # earlystop = EarlyStopping(tolerance=5,delta=0.5)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=epochs * len(train_dataloader)
    )
    avg_train_loss = []
    avg_val_loss = []
    max_val_loss = float('+inf')

    print('*** Initiate Training Phase *** ')
    print()
    for epoch in range(epochs):
        progress = tqdm(train_dataloader, total=len(train_dataloader), desc='Epoch [{}/{}]'.format(epoch, epochs - 1))
        train_loss = 0
        for idx, (tokens, mask, prefix) in enumerate(train_dataloader):
            model.train()
            model.zero_grad()
            tokens, mask, prefix = tokens.to(device), mask.to(device), prefix.to(device, dtype=torch.float32)

            outputs = model(tokens, prefix, mask)
            logits = outputs.logits[:, train_dataset.prefix_length - 1: -1]
            loss = nnf.cross_entropy(logits.reshape(-1, logits.shape[-1]), tokens.flatten(), ignore_index=0)
            train_loss = train_loss + loss.item()
            loss.backward()
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            progress.set_postfix({"Batch Train Loss": loss.item()})
            progress.update()

        progress.close()

        epoch_avg_train_loss = train_loss / len(train_dataloader)
        avg_train_loss.append(epoch_avg_train_loss)
        print('*** In Epoch {} the average train loss : {} ***'.format(epoch, epoch_avg_train_loss))

        epoch_avg_val_loss = apply_validation(model, val_dataloader, epoch, prefix_length=val_dataset.prefix_length)
        avg_val_loss.append(epoch_avg_val_loss)

        # earlystop(epoch_avg_train_loss,epoch_avg_val_loss)
        # if earlystop.early_stop:
        #     print('*** Exit Training due to Early Stopping ( at Epoch {} ) ***'.format(epoch))
        #     break

        if epoch_avg_val_loss < max_val_loss:
            # best_model = copy.deepcopy(model)
            max_val_loss = epoch_avg_val_loss

            # torch.save(best_model.state_dict(), os.path.join(output_dir, f"{model_name}_bestmodel.pt"))
            print(f'Best Validation loss  : {epoch_avg_val_loss}')

        if epoch % myconfig.get('save_every') == 0 or epoch == epochs - 1:
            torch.save(
                model.state_dict(),
                os.path.join(output_dir, f"{model_name}-{epoch:03d}.pt"),
            )

    # fig, axes = plt.subplots(1, figsize=(15, 15))
    # plt.plot([e for e in range(epochs)], avg_train_loss, color='b', linestyle='-', label='Training loss')
    # plt.plot([e for e in range(epochs)], avg_val_loss, color='r', linestyle='--', label='Validation loss')
    # plt.title('Training Loss & Epochs', fontsize=16)
    # plt.xlabel('Epochs', fontsize=16)
    # plt.ylabel('Loss', fontsize=16)
    # plt.legend()
    # plt.savefig('./train_val_loss_{}.png'.format(myconfig.get('model_name')))
    return model


def generate_topk(
        model,
        tokenizer,
        tokens=None,
        prompt=None,
        embed=None,
        entry_count=1,
        entry_length=50,  # maximum number of words
        top_p=0.8,
        temperature=1.0,
        stop_token: str = ".",
):
    stop_token_index = tokenizer.encode(stop_token)[0]
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    batch_size = embed.shape[0]
    eos_token_index = tokenizer.eos_token_id
    seq_lengths = torch.ones(batch_size, device=device)
    is_stopped = torch.zeros(batch_size, device=device, dtype=torch.bool)
    with torch.no_grad():
        for entry_idx in range(entry_count):
            if embed is not None:
                generated = embed
            else:
                if tokens is None:
                    tokens = torch.tensor(tokenizer.encode(prompt))
                    tokens = tokens.unsqueeze(0).to(device)

                generated = model.gpt.transformer.wte(tokens)
            for i in range(entry_length):
                outputs = model.gpt(inputs_embeds=generated)
                logits = outputs.logits
                logits = logits[:, -1, :] / (temperature if temperature > 0 else 1.0)
                logits = logits.softmax(-1).log()
                scores, next_tokens = logits.topk(1, -1)
                if tokens is None:
                    tokens = next_tokens
                else:
                    tokens = torch.cat((tokens, next_tokens), dim=1)
                next_token_embed = model.gpt.transformer.wte(next_tokens)
                generated = torch.cat((generated, next_token_embed), dim=1)

                seq_lengths[~is_stopped] += 1
                is_stopped = is_stopped + next_tokens.eq(stop_token_index).squeeze() + \
                             next_tokens.eq(eos_token_index).squeeze()
                if is_stopped.all():
                    break

            output_list = tokens.cpu().numpy()
            output_texts = [
                tokenizer.decode(output[: int(length)], skip_special_tokens=True)
                for output, length in zip(output_list, seq_lengths)
            ]
    return output_texts


def merge(list1, list2):
    assert len(list1) == len(list2)
    merged_list = [(list1[i], list2[i]) for i in range(0, len(list1))]
    return merged_list


def group_reference_captions(gt_image_ids, gt_captions):
    mrg = merge(gt_image_ids, gt_captions)
    temp_dict = {}
    for key, value in mrg:
        if key in temp_dict:
            temp_dict[key].append(value)
        else:
            temp_dict[key] = [value]
    return temp_dict


def validation_generation(model, val_dataset, batch_size, weights_path=None):
    start_time = time.time()
    full_gt_dict = {}
    gen = {}
    gts = {}
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, drop_last=False)
    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    generated_captions = []

    gt_image_ids = val_dataset.image_ids
    gt_captions = val_dataset.captions
    group_dict = group_reference_captions(gt_image_ids, gt_captions)

    model.load_state_dict(torch.load(weights_path, map_location=device))
    model = model.to(device)
    model.eval()

    for (captions, mask, prefix) in tqdm(val_dataloader, total=len(val_dataloader), desc='Generate Captions'):
        captions, mask, prefix = captions.to(device), mask.to(device), prefix.to(device, dtype=torch.float32)

        with torch.no_grad():
            prefix_embed = model.clip_project(prefix)
            output = generate_topk(model, tokenizer, embed=prefix_embed)
            generated_captions.extend(output)

    assert len(generated_captions) == len(gt_captions)

    for i, (pred_a, gt_a) in enumerate(zip(generated_captions, gt_captions)):
        gen[str(i)] = [pred_a]
        gts[str(i)] = [gt_a]
        full_gt_dict[str(i)] = {'image_id': gt_image_ids[i],
                                'original_caption': gt_captions[i],
                                'generated_caption': pred_a,
                                'reference_captions': group_dict.get(gt_image_ids[i])
                                }

    with open("./dict_coco_80K.json", "w") as outfile:
        json.dump(full_gt_dict, outfile)

    end_time = time.time()
    total = round((end_time - start_time) / 60, 2)
    print('*** The Validation is finished in {} minutes ***'.format(total))
    return gen, gts, full_gt_dict



def main():
    myconfig = {
        'epochs': 10,
        'batch_size': 32,
        'train_data': '/scratch/chris.morfopoulos/data/coco/combined_gen_clipscore_80k_feat_train_ic.pkl',
        'val_data': '/scratch/chris.morfopoulos/data/coco/clip_feat_ViT-B_32_val_ic.pkl',
        'out_dir': '/scratch/chris.morfopoulos/temp_code/ablation_/coco_80K',
        'save_every': 1,
        'prefix_length': 10,
        'prefix_length_clip': 10,
        'only_prefix': True,
        'mapping_type': 'transformer',
        'num_layers': 8,
        'is_rn': False,
        'normalize_prefix': False,
        'model_name': 'coco_80K',
        'weights_path': '/scratch/chris.morfopoulos/temp_code/ablation_/coco_80K/coco_80K_bestmodel.pt'

    }
    print('Logging args **** ' + str(myconfig))
    prefix_dim = 640 if myconfig.get('is_rn') else 512
    print()
    train_dataset = ClipCocoDataset(myconfig.get('train_data'), myconfig.get('prefix_length'),
                                    normalize_prefix=myconfig.get('normalize_prefix'))
    val_dataset = ClipCocoDataset(myconfig.get('val_data'), myconfig.get('prefix_length'),
                                  normalize_prefix=myconfig.get('normalize_prefix'))
    mapping_type = {'mlp': MappingType.MLP, 'transformer': MappingType.Transformer}[myconfig.get('mapping_type')]
    print()
    model = ClipCaptionPrefix(myconfig.get('prefix_length'), clip_length=myconfig.get('prefix_length_clip'),
                              prefix_size=prefix_dim, num_layers=myconfig.get('num_layers'),
                              mapping_type=mapping_type)
    train(model, train_dataset, val_dataset, myconfig, output_dir=myconfig.get('out_dir'),
          model_name=myconfig.get('model_name'))

    gen, gts, full_gt_dict = validation_generation(model, val_dataset, batch_size=32, weights_path=myconfig.get('weights_path'))


if __name__ == '__main__':
    main()
