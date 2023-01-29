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
from typing import Tuple, Optional, Union
import matplotlib.pyplot as plt



class MappingType(Enum):
    MLP = 'mlp'
    Transformer = 'transformer'


class ClipCocoDataset(Dataset):

    def __len__(self) -> int:
        return len(self.captions_tokens)

    def pad_tokens(self, item: int):
        tokens = self.captions_tokens[item]
        temp_ans = self.answers[item]
        temp_q = self.questions[item]
        tokenized_answer =  torch.tensor(self.tokenizer.encode(temp_ans) , dtype=torch.int64)
        q_range = len(self.tokenizer.encode(temp_q))
        a_range = len(self.tokenizer.encode(temp_ans))
        rest_range = self.max_seq_len - q_range - a_range
        if rest_range>=0:
            need_pred = q_range*[0] + a_range*[1] + rest_range*[0]
        elif rest_range<0:
            print('SOOS')
            need_pred = self.max_seq_len*[0]

        padding = self.max_seq_len - tokens.shape[0]
        if padding > 0:
            tokens = torch.cat((tokens, torch.zeros(padding, dtype=torch.int64) - 1))
            self.captions_tokens[item] = tokens
        elif padding < 0:
            tokens = tokens[:self.max_seq_len]
            self.captions_tokens[item] = tokens
        # A boolean tensor that is True where input is greater than or equal to other and False elsewhere
        # mask = tokens.ge(0)  # mask is zero where we out of sequence
        # tokens[~mask] = 0
        mask = torch.FloatTensor(need_pred)

        omask = tokens.ge(0)  # mask is zero where we out of sequence
        tokens[~omask] = 0

        # SOS
        mask = torch.cat((torch.ones(self.prefix_length), mask), dim=0)  # adding prefix mask
        return tokens, mask , tokenized_answer

    def __getitem__(self, item: int) -> Tuple[torch.Tensor, ...]:
        tokens, mask, tokenized_answer = self.pad_tokens(item)
        prefix = self.prefixes[self.caption2embedding[item]]
        if self.normalize_prefix:
            prefix = prefix.float()
            prefix = prefix / prefix.norm(2, -1)

        # tokenized caption, mask attention , (prefix --> actual image)
        return tokens, mask, prefix, tokenized_answer

    def __init__(self, data_path: str,  prefix_length: int, gpt2_type: str = "gpt2",
                 normalize_prefix=False):
        self.tokenizer = GPT2Tokenizer.from_pretrained(gpt2_type)
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
        self.answers = [caption['answer'] for caption in captions_raw]
        self.questions = [caption['question'] for caption in captions_raw]
        ##
        self.captions_tokens = []
        self.caption2embedding = []
        self.temp_answers_tens = []
        max_seq_len = 0
        for i,caption in enumerate(captions_raw):
            # tokenize to caption
            self.captions_tokens.append(torch.tensor(self.tokenizer.encode(caption['question']+' '+caption['answer']) , dtype=torch.int64))
            # clip_embedding einai to sequential ID !!
            self.caption2embedding.append(caption["clip_embedding"])
            max_seq_len = max(max_seq_len, self.captions_tokens[-1].shape[0])
            # temp = torch.tensor(self.tokenizer.encode(caption['answer']), dtype=torch.int64)
            # self.temp_answers_tens.append(temp)
            # max_seq_len = max(max_seq_len, temp.shape[0])
        with open(f"{data_path[:-4]}_tokens.pkl", 'wb') as f:
            pickle.dump([self.captions_tokens, self.caption2embedding, self.answers, self.questions,max_seq_len], f)
        # all_len = torch.tensor([len(self.temp_answers_tens[i]) for i in range(len(self))]).float()
        # self.max_seq_len = min(int(all_len.mean() + all_len.std() * 10), int(all_len.max()))

        all_len = torch.tensor([len(self.captions_tokens[i]) for i in range(len(self))]).float()
        self.max_seq_len = min(int(all_len.mean() + all_len.std() * 10), int(all_len.max()))
        # self.max_seq_len = max_seq_len
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
            if i % 2 == 0 and self.enc_dec: # cross
                x = layer(x, y)
            elif self.enc_dec:  # self
                x = layer(x, x, mask)
            else:  # self or cross
                x = layer(x, y, mask)
        return x

    def __init__(self, dim_self: int, num_heads: int, num_layers: int, dim_ref: Optional[int] = None,
                 mlp_ratio: float = 2., act=nnf.relu, norm_layer: nn.Module = nn.LayerNorm, enc_dec: bool = False):
        super(Transformer, self).__init__()
        print('Initiate Transformer *** with 8 Transformer layers ! ')
        dim_ref = dim_ref if dim_ref is not None else dim_self
        self.enc_dec = enc_dec
        if enc_dec:
            num_layers = num_layers * 2
        layers = []
        for i in range(num_layers):
            if i % 2 == 0 and enc_dec:  # cross
                layers.append(TransformerLayer(dim_self, dim_ref, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
            elif enc_dec:  # self
                layers.append(TransformerLayer(dim_self, dim_self, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
            else:  # self or cross
                   # dim self 768
                layers.append(TransformerLayer(dim_self, dim_ref, num_heads, mlp_ratio, act=act, norm_layer=norm_layer))
        self.layers = nn.ModuleList(layers)


class TransformerMapper(nn.Module):

    def forward(self, x):
        # apo 1 x 512
        # 1 x 10 x 768 Both?
        x = self.linear(x).view(x.shape[0], self.clip_length, -1)
        prefix = self.prefix_const.unsqueeze(0).expand(x.shape[0], * self.prefix_const.shape)
        prefix = torch.cat((x, prefix), dim=1)
        # TODO
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

        # ta tokens twn captions - prefix clip embedings (img)  -  maska

        # size of tokens --> 1 x 30
        # size word embe of gpt ---> 1 x 30 x 768
        embedding_text = self.gpt.transformer.wte(tokens)


        # execute clip project with prefix!!
        # tranform to   --> 1 x 10 x 768 (self.gpt_embedding_size 768)
        prefix_projections = self.clip_project(prefix).view(-1, self.prefix_length, self.gpt_embedding_size)
        # kanoume concatenate ta prefix_projections & embedding_text
        # concat 1 x 40 x 768
        embedding_cat = torch.cat((prefix_projections, embedding_text), dim=1)
        #TODO
        out = self.gpt(inputs_embeds=embedding_cat, labels=None, attention_mask=mask)
        print()
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
                                                  clip_length,num_layers)


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


def train(dataset: ClipCocoDataset, model: ClipCaptionModel, args,
          lr: float = 2e-5, warmup_steps: int = 5000, output_dir: str = ".", output_prefix: str = ""):

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    batch_size = args.bs
    epochs = args.epochs
    # test_tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    model = model.to(device)
    model.train()
    optimizer = AdamW(model.parameters(), lr=lr)
    train_dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=epochs * len(train_dataloader)
    )
    #  always --> mask  1x40 , tokens 1x30, 1x512 img ... with respect to the dataset

    epoch_train_loss = []
    for epoch in range(epochs):
        print(f">>> Training epoch {epoch}")
        sys.stdout.flush()
        progress = tqdm(total=len(train_dataloader), desc=output_prefix)
        sum_train_loss = 0
        for idx, (tokens, mask, prefix , tokenized_answer) in enumerate(train_dataloader):

            model.zero_grad()
            tokens, mask, prefix , tokenized_answer = tokens.to(device), mask.to(device), prefix.to(device, dtype=torch.float32) , tokenized_answer.to(device)

            outputs = model(tokens, prefix, mask)
            logits = outputs.logits[:, dataset.prefix_length - 1: -1]
            modified_mask_v2 = mask.squeeze()[ 10: ]
            bool_mask = modified_mask_v2.ge(1)
            final_logits = logits.reshape(-1, logits.shape[-1])
            new_final_logits = final_logits[bool_mask]

            loss = nnf.cross_entropy(new_final_logits, tokenized_answer.flatten(), ignore_index=0)
            sum_train_loss = sum_train_loss + loss.item()
            loss.backward()
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            progress.set_postfix({"loss": loss.item()})
            progress.update()

        epoch_train_loss.append(sum_train_loss)
        progress.close()
        if epoch % args.save_every == 0 or epoch == epochs - 1:
            torch.save(
                model.state_dict(),
                os.path.join(output_dir, f"{output_prefix}-{epoch:03d}.pt"),
            )

    fig, axes = plt.subplots(1, figsize=(15, 15))
    plt.plot(epochs, epoch_train_loss, color='b', linestyle='-', label='Training loss')
    plt.title('Training Loss & Epochs', fontsize=16)
    plt.xlabel('Epochs', fontsize=16)
    plt.ylabel('Loss', fontsize=16)
    plt.legend()
    plt.savefig('./epoch_train_loss')

    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='./data/coco/oscar_split_ViT-B_32_trainy.pkl')
    parser.add_argument('--out_dir', default='./checkpoints')
    parser.add_argument('--prefix', default='coco_prefix', help='prefix for saved filenames')
    parser.add_argument('--epochs', type=int, default=2)
    parser.add_argument('--save_every', type=int, default=1)
    parser.add_argument('--prefix_length', type=int, default=10)
    parser.add_argument('--prefix_length_clip', type=int, default=10)
    parser.add_argument('--bs', type=int, default=40)
    parser.add_argument('--only_prefix', dest='only_prefix', action='store_true')
    parser.add_argument('--mapping_type', type=str, default='transformer', help='mlp/transformer')
    parser.add_argument('--num_layers', type=int, default=8)
    parser.add_argument('--is_rn', dest='is_rn', action='store_true')
    parser.add_argument('--normalize_prefix', dest='normalize_prefix', action='store_true')
    args = parser.parse_args()
    print('args **** ' + str(args))
    print()
    prefix_length = args.prefix_length
    # for ViT B 512 , ViT L 768, RESNET 640?
    prefix_dim = 640 if args.is_rn else 512
    # args.data = './data/coco/oscar_split_ViT-B_32_trainy_vqa.pkl'
    dataset = ClipCocoDataset(args.data, prefix_length, normalize_prefix=args.normalize_prefix)
    args.mapping_type = {'mlp': MappingType.MLP, 'transformer': MappingType.Transformer}[args.mapping_type]


    args.only_prefix = True
    if args.only_prefix:
        # 10 - 10 - 512 (fixed) - #layers 8 - transformer
        model = ClipCaptionPrefix(prefix_length, clip_length=args.prefix_length_clip,
                                  prefix_size=prefix_dim,num_layers=args.num_layers,
                                  mapping_type=args.mapping_type)
    else:
        model = ClipCaptionModel(prefix_length, clip_length=args.prefix_length_clip, prefix_size=prefix_dim,
                                  num_layers=args.num_layers, mapping_type=args.mapping_type)
        sys.stdout.flush()
    train(dataset, model, args, output_dir=args.out_dir, output_prefix=args.prefix)


if __name__ == '__main__':
    main()