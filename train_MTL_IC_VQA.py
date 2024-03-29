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
import copy

class MappingType(Enum):
    MLP = 'mlp'
    Transformer = 'transformer'


class ClipCocoDataset_IC(Dataset):
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
        mask = torch.cat((torch.ones(self.prefix_length), mask), dim=0)  # adding prefix mask
        return tokens, mask

    def __getitem__(self, item: int) -> Tuple[torch.Tensor, ...]:
        tokens, mask = self.pad_tokens(item)
        prefix = self.prefixes[self.caption2embedding[item]]
        if self.normalize_prefix:
            prefix = prefix.float()
            prefix = prefix / prefix.norm(2, -1)
        return tokens, mask, prefix

    def __init__(self, data_path: str, prefix_length: int, gpt2_type: str = "gpt2",
                 normalize_prefix=False):
        self.tokenizer = GPT2Tokenizer.from_pretrained(gpt2_type)
        self.prefix_length = prefix_length
        self.normalize_prefix = normalize_prefix
        with open(data_path, 'rb') as f:
            all_data = pickle.load(f)
        print("Data size is %0d" % len(all_data["clip_embedding"]))
        sys.stdout.flush()
        # prefixes einai ta clip embeddings
        # print(all_data["clip_embedding"])
        # print(type(all_data["clip_embedding"]))
        # print('all_data["clip_embedding"]')
        print()
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
        # self.max_seq_len = max_seq_len
        all_len = torch.tensor([len(self.captions_tokens[i]) for i in range(len(self))]).float()
        self.max_seq_len = min(int(all_len.mean() + all_len.std() * 10), int(all_len.max()))


class ClipCocoDataset_VQA(Dataset):
    def __len__(self) -> int:
        return len(self.captions_tokens)

    def pad_tokens(self, item: int):
        tokens = self.captions_tokens[item]
        temp_ans = self.answers[item]
        temp_q = self.questions[item]
        tokenized_answer = torch.tensor(self.tokenizer.encode(temp_ans), dtype=torch.int64)
        q_range = len(self.tokenizer.encode(temp_q))
        a_range = len(self.tokenizer.encode(temp_ans)) + 1
        rest_range = self.max_seq_len - q_range - a_range
        if rest_range >= 0:
            need_pred = q_range * [0] + a_range * [1] + rest_range * [0]
            need_pred_4gpt = q_range * [1] + a_range * [1] + rest_range * [0]
        elif rest_range < 0:
            # TODO
            # print('SOOS')
            need_pred = self.max_seq_len * [0]
            need_pred_4gpt = self.max_seq_len * [0]

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
        mask4gpt = torch.FloatTensor(need_pred_4gpt)

        omask = tokens.ge(0)  # mask is zero where we out of sequence
        tokens[~omask] = 0

        # SOS
        mask = torch.cat((torch.ones(self.prefix_length), mask), dim=0)  # adding prefix mask
        mask4gpt = torch.cat((torch.ones(self.prefix_length), mask4gpt), dim=0)  # adding prefix mask
        return tokens, mask, mask4gpt

    def __getitem__(self, item: int) -> Tuple[torch.Tensor, ...]:
        tokens, mask, mask4gpt = self.pad_tokens(item)
        prefix = self.prefixes[self.caption2embedding[item]]
        if self.normalize_prefix:
            prefix = prefix.float()
            prefix = prefix / prefix.norm(2, -1)

        # tokenized caption, mask attention , (prefix --> actual image)
        return tokens, mask, mask4gpt, prefix

    def __init__(self, data_path: str, prefix_length: int, gpt2_type: str = "gpt2",
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
        # self.temp_answers_tens = []
        eos = self.tokenizer.eos_token_id
        max_seq_len = 0
        max_ans_len = 0
        for i, caption in enumerate(captions_raw):
            # tokenize to caption
            self.captions_tokens.append(
                torch.tensor(self.tokenizer.encode(caption['question'] + ' ' + caption['answer']) + [eos],
                             dtype=torch.int64))
            # clip_embedding einai to sequential ID !!
            self.caption2embedding.append(caption["clip_embedding"])
            max_seq_len = max(max_seq_len, self.captions_tokens[-1].shape[0])

            temp = torch.tensor(self.tokenizer.encode(caption['answer']), dtype=torch.int64)
            max_ans_len = max(max_ans_len, temp.shape[0])

        all_len = torch.tensor([len(self.captions_tokens[i]) for i in range(len(self))]).float()
        self.max_seq_len = min(int(all_len.mean() + all_len.std() * 10), int(all_len.max()))
        self.max_ans_len = max_ans_len
        print('max_seq_len of whole tokens :  ' + str(self.max_seq_len))
        print('max_ans_len of answers :  ' + str(self.max_ans_len))


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


def apply_validation_ic(model, val_dataloader, epoch, prefix_length):
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
    print('*** In Epoch {} the average validation loss for IC : {} ***'.format(epoch, avg_val_loss))
    return avg_val_loss


def apply_validation_vqa(model, val_dataloader, epoch, prefix_length):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    val_loss = 0
    model.eval()
    for idx, (tokens, mask, mask4gpt, prefix) in enumerate(val_dataloader):
        tokens, mask, mask4gpt, prefix = tokens.to(device), mask.to(device),\
                                        mask4gpt.to(device), prefix.to(device,dtype=torch.float32)
        with torch.no_grad():
            outputs = model(tokens, prefix, mask4gpt)
            logits = outputs.logits[:, prefix_length - 1: -1]
            new_mask = mask[:, 10:]
            bool_mask = new_mask.ge(1).view(-1)
            final_logits = logits.reshape(-1, logits.shape[-1])
            finally_tok = tokens.view(-1)
            loss = nnf.cross_entropy(final_logits[bool_mask], finally_tok[bool_mask], ignore_index=0)
            val_loss = val_loss + loss.item()

    avg_val_loss = val_loss / len(val_dataloader)
    print('*** In Epoch {} the average validation loss for VQA : {} ***'.format(epoch, avg_val_loss))
    return avg_val_loss


def train_ic(model, token_ic, mask_ic, prefix_ic):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    prefix_length = 10
    tokens, mask, prefix = token_ic.to(device), mask_ic.to(device), prefix_ic.to(device, dtype=torch.float32)
    outputs = model(tokens, prefix, mask)
    logits = outputs.logits[:, prefix_length - 1: -1]
    temp_logits = logits.reshape(-1, logits.shape[-1])
    temp_tokens = tokens.flatten()
    return temp_logits , temp_tokens


def train_vqa(model, tokens_vqa, mask_vqa, mask4gpt_vqa, prefix_vqa):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    prefix_length = 10
    tokens, mask, mask4gpt, prefix = tokens_vqa.to(device), mask_vqa.to(device), mask4gpt_vqa.to(device), \
        prefix_vqa.to(device, dtype=torch.float32)
    outputs = model(tokens, prefix, mask4gpt)
    logits = outputs.logits[:, prefix_length - 1: -1]
    new_mask = mask[:, 10:]
    bool_mask = new_mask.ge(1).view(-1)
    final_logits = logits.reshape(-1, logits.shape[-1])
    finally_tok = tokens.view(-1)
    temp_logits = final_logits[bool_mask]
    temp_tokens = finally_tok[bool_mask]
    return temp_logits , temp_tokens

def main():
    myconfig = {
        'epochs': 10,
        'batch_size': 32,
        'train_data_ic': './data/vizwiz/combined_gen_clipscore_feat_train_ic.pkl',
        'val_data_ic': './data/vizwiz/clip_feat_ViT-B_32_val_ic.pkl',
        'train_data_vqa': './data/vizwiz/clip_feat_ViT-B_32_train_vqa.pkl',
        'val_data_vqa': './data/vizwiz/clip_feat_ViT-B_32_val_vqa.pkl',
        'out_dir': './MTL_diffu_model_vizwiz',
        'weight_loss_ic': 0.5,
        'weight_loss_vqa': 0.5,
        'save_every': 1,
        'prefix_length': 10,
        'prefix_length_clip': 10,
        'only_prefix': True,
        'mapping_type': 'transformer',
        'num_layers': 8,
        'is_rn': False,
        'normalize_prefix': False,
        'model_name': 'MTL_diffu_model_vizwiz',
        'weights_path': ''

    }
    print('Logging args **** ' + str(myconfig))
    prefix_dim = 640 if myconfig.get('is_rn') else 512
    print()

    mapping_type = {'mlp': MappingType.MLP,
                    'transformer': MappingType.Transformer}[myconfig.get('mapping_type')]

    model = ClipCaptionPrefix(myconfig.get('prefix_length'),
                              clip_length=myconfig.get('prefix_length_clip'),
                              prefix_size=prefix_dim,
                              num_layers=myconfig.get('num_layers'),
                              mapping_type=mapping_type)

    train_dataset_ic = ClipCocoDataset_IC(myconfig.get('train_data_ic'),
                                          myconfig.get('prefix_length'),
                                          normalize_prefix=myconfig.get('normalize_prefix'))
    val_dataset_ic = ClipCocoDataset_IC(myconfig.get('val_data_ic'),
                                        myconfig.get('prefix_length'),
                                        normalize_prefix=myconfig.get('normalize_prefix'))

    train_dataset_vqa = ClipCocoDataset_VQA(myconfig.get('train_data_vqa'),
                                            myconfig.get('prefix_length'),
                                            normalize_prefix=myconfig.get('normalize_prefix'))
    val_dataset_vqa = ClipCocoDataset_VQA(myconfig.get('val_data_vqa'),
                                          myconfig.get('prefix_length'),
                                          normalize_prefix=myconfig.get('normalize_prefix'))

    train_dataloader_ic = DataLoader(train_dataset_ic, batch_size=myconfig.get('batch_size'), shuffle=False,
                                     drop_last=False)
    val_dataloader_ic = DataLoader(val_dataset_ic, batch_size=myconfig.get('batch_size'), shuffle=False,
                                   drop_last=False)

    train_dataloader_vqa = DataLoader(train_dataset_vqa, batch_size=myconfig.get('batch_size'), shuffle=False,
                                      drop_last=False)
    val_dataloader_vqa = DataLoader(val_dataset_vqa, batch_size=myconfig.get('batch_size'), shuffle=False,
                                    drop_last=False)

    print('*** Lengths ***')
    print(len(train_dataset_ic))
    print(len(train_dataset_vqa))
    print('*** Lengths ***')
    print()

    avg_train_loss_ic = []
    avg_train_loss_vqa = []
    avg_val_loss_ic = []
    avg_val_loss_vqa = []

    counter_batch_ic = 0
    counter_batch_vqa = 0
    train_loss_ic = 0
    train_loss_vqa = 0
    lr = 2e-5
    warmup_steps = 5000
    epochs = myconfig.get('epochs')
    output_dir = myconfig.get('out_dir')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)
    optimizer = AdamW(model.parameters(), lr=lr)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps,
        num_training_steps=epochs * len(train_dataloader_vqa))

    for epoch in range(epochs):
        t_ic_dataloader = iter(train_dataloader_ic)
        t_vqa_dataloader = iter(train_dataloader_vqa)
        progress = tqdm(train_dataloader_ic, total=len(train_dataloader_ic), desc='Epoch [{}/{}]'.format(epoch,epochs-1))
        while True:
            model.train()
            model.zero_grad()
            try:
                batch_ic = next(t_ic_dataloader)
                batch_vqa = next(t_vqa_dataloader)
            except StopIteration:
                break

            (token_ic, mask_ic, prefix_ic) = batch_ic
            (tokens_vqa, mask_vqa, mask4gpt_vqa, prefix_vqa) = batch_vqa
            temp_logits_ic, temp_tokens_ic = train_ic(model, token_ic, mask_ic, prefix_ic)
            temp_logits_vqa, temp_tokens_vqa = train_vqa(model, tokens_vqa, mask_vqa, mask4gpt_vqa, prefix_vqa)

            weight_ic = myconfig.get('weight_loss_ic')
            weight_vqa = myconfig.get('weight_loss_vqa')

            loss_ic = nnf.cross_entropy(temp_logits_ic, temp_tokens_ic, ignore_index = 0)
            loss_vqa = nnf.cross_entropy(temp_logits_vqa, temp_tokens_vqa, ignore_index = 0)
            loss = (weight_ic * loss_ic) + (weight_vqa * loss_vqa)

            loss.backward()
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()

            train_loss_ic = train_loss_ic + loss_ic.item()
            train_loss_vqa = train_loss_vqa + loss_vqa.item()

            counter_batch_ic += mask_ic.shape[0]
            counter_batch_vqa += mask_vqa.shape[0]
            progress.set_postfix({"Batch train_loss_ic": loss_ic.item(),
                                  "Batch train_loss_vqa": loss_vqa.item()})
            progress.update()

        progress.close()
        epoch_avg_train_loss_ic = train_loss_ic / len(train_dataloader_ic)
        epoch_avg_train_loss_vqa = train_loss_vqa / len(train_dataloader_ic)

        avg_train_loss_ic.append(epoch_avg_train_loss_ic)
        avg_train_loss_vqa.append(epoch_avg_train_loss_vqa)

        print()
        print('Trained for total of {} samples in Image Captioning (IC).'.format(counter_batch_ic))
        print('Trained for total of {} samples in Visual Question Answering (VQA).'.format(counter_batch_vqa))
        print()

        epoch_avg_val_loss_ic = apply_validation_ic(model, val_dataloader_ic, epoch, prefix_length=10)
        epoch_avg_val_loss_vqa = apply_validation_vqa(model, val_dataloader_vqa, epoch, prefix_length=10)

        avg_val_loss_ic.append(epoch_avg_val_loss_ic)
        avg_val_loss_vqa.append(epoch_avg_val_loss_vqa)

        if epoch % myconfig.get('save_every') == 0 or epoch == epochs - 1:
            torch.save(
                model.state_dict(),
                os.path.join(output_dir, f"{myconfig.get('model_name')}-{epoch:03d}.pt"),
            )
    print()
    print('####')
    print(avg_train_loss_ic)
    print(avg_train_loss_vqa)
    print(avg_val_loss_ic)
    print(avg_val_loss_vqa)
    print('####')


if __name__ == '__main__':
    main()
