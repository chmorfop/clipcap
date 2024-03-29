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
from torch.nn.parallel import DistributedDataParallel as DDP

import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.distributed import init_process_group, destroy_process_group

torch.multiprocessing.set_sharing_strategy('file_system')


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
        # TODO
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


def apply_validation(model, val_dataloader, epoch,rank, prefix_length):
    # device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    val_loss = 0
    model.eval()
    for idx, (tokens, mask, mask4gpt, prefix) in enumerate(val_dataloader):
        tokens, mask, mask4gpt, prefix = tokens.to(rank), mask.to(rank), mask4gpt.to(rank), prefix.to(rank,dtype=torch.float32)
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


def generate_per_batch(model, prefix, question, batch_size,masky):
    tokens = None
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    stop_token_index = tokenizer.encode('.')[0]
    eos_token_index = tokenizer.eos_token_id
    max_length = 67
    temperature = 1.0
    seq_lengths = torch.ones(batch_size, device=device)
    is_stopped = torch.zeros(batch_size, device=device, dtype=torch.bool)
    with torch.no_grad():
        embed = model.clip_project(prefix)
        embedding_text = model.gpt.transformer.wte(question)
        generated = torch.cat((embed, embedding_text), dim=1)
        for i in range(max_length):
            outputs = model.gpt(inputs_embeds=generated,attention_mask=masky)

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
            masky = torch.cat((masky, torch.ones((batch_size, 1), dtype=torch.float)), dim=1)

            seq_lengths[~is_stopped] += 1
            is_stopped = is_stopped + next_tokens.eq(stop_token_index).squeeze() + \
                         next_tokens.eq(eos_token_index).squeeze()
            if is_stopped.all():
                break

        output_list = tokens.cpu().numpy()
        output_texts = [
            tokenizer.decode(output[: int(length)], skip_special_tokens=True)
            for output, length in zip(output_list, seq_lengths)]
    return output_texts


def validation_generation(model, val_dataset, weights_path=None):
    full_gt_dict = {}
    gen = {}
    gts = {}
    batch_size = 4
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, drop_last=False)
    gt_image_ids = val_dataset.image_ids
    gt_questions = val_dataset.questions
    gt_answers = val_dataset.answers
    model.load_state_dict(torch.load(weights_path, map_location=device))
    model = model.to(device)
    model.eval()
    predicted_answers = []

    for (captions, mask, mask4gpt, prefix) in tqdm(val_dataloader, total=len(val_dataloader),
                                                   desc='Generate Captions/Answers'):
        captions, mask, mask4gpt, prefix = captions.to(device), mask.to(device), mask4gpt.to(device), prefix.to(device,
                                                                                                                dtype=torch.float32)
        temp_question_mask = torch.logical_xor(mask[:, 10:], mask4gpt[:, 10:]).float()
        masky = torch.cat((torch.ones((batch_size, 10),dtype=torch.float),temp_question_mask), dim=1)

        new_mask = mask[:, 10:].ge(1)
        new_mask4gpt = mask4gpt[:, 10:].ge(1)
        question_mask = torch.logical_xor(new_mask, new_mask4gpt)
        questions = captions * question_mask

        # for b in range(batch_size):
        #     new_mask = mask[:, 10:].ge(1)
        #     new_mask4gpt = mask4gpt[:, 10:].ge(1)
        #     question_mask = torch.logical_xor(new_mask, new_mask4gpt)
        #     questions = captions * question_mask
        # question = questions.squeeze()
        # question = question[question.nonzero()].squeeze()

        output_texts = generate_per_batch(model, prefix, questions, batch_size, masky)
        predicted_answers.extend(output_texts)

    assert len(predicted_answers) == len(gt_answers)
    print('# Generations : ' + str(len(gt_answers)))

    for i, (pred_a, gt_a) in enumerate(zip(predicted_answers, gt_answers)):
        gen[str(i)] = pred_a
        gts[str(i)] = gt_a
        full_gt_dict[str(i)] = {'image_id': gt_image_ids[i],
                                'question': gt_questions[i],
                                'answer': gt_a,
                                'predicted_answer': pred_a
                                }

    with open("./full_gt_dict.json", "w") as outfile:
        json.dump(full_gt_dict, outfile)

    return gen, gts, full_gt_dict


def train(rank: int , model: ClipCaptionModel, train_dataset: ClipCocoDataset,
          val_dataset: ClipCocoDataset, myconfig, output_dir: str , model_name: str , world_size: int):
    lr = 2e-5
    warmup_steps = 5000
    #device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    model = model.cuda()
    model = DDP(model, device_ids=[rank])

    optimizer = AdamW(model.module.parameters(), lr=lr)
    batch_size = myconfig.get('batch_size')
    epochs = myconfig.get('epochs')

    train_sampler = sampler = DistributedSampler(train_dataset, shuffle=True)
    train_dataloader = DataLoader(train_dataset, batch_size=batch_size, drop_last=False, sampler=train_sampler,
                                  pin_memory=True)

    val_sampler = sampler = DistributedSampler(val_dataset, shuffle=False)
    val_dataloader = DataLoader(val_dataset, batch_size=batch_size, drop_last=False, sampler=val_sampler,
                                pin_memory=True)

    # earlystop = EarlyStopping(tolerance=5, delta=0.5)
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
        for idx, (tokens, mask, mask4gpt, prefix) in enumerate(train_dataloader):
            model.train()
            model.zero_grad()
            tokens, mask, mask4gpt, prefix = tokens.to(rank), mask.to(rank), mask4gpt.to(rank), prefix.to(rank,dtype=torch.float32)

            outputs = model(tokens, prefix, mask4gpt)
            logits = outputs.logits[:, train_dataset.prefix_length - 1: -1]
            new_mask = mask[:, 10:]
            bool_mask = new_mask.ge(1).view(-1)
            final_logits = logits.reshape(-1, logits.shape[-1])
            finally_tok = tokens.view(-1)

            loss = nnf.cross_entropy(final_logits[bool_mask], finally_tok[bool_mask], ignore_index=0)
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

        epoch_avg_val_loss = apply_validation(model, val_dataloader, epoch, rank=rank,prefix_length=val_dataset.prefix_length)
        avg_val_loss.append(epoch_avg_val_loss)

        # earlystop(epoch_avg_train_loss, epoch_avg_val_loss)
        # if earlystop.early_stop:
        #     print('*** Exit Training due to Early Stopping ( at Epoch {} ) ***'.format(epoch))
        #     break

        if epoch_avg_val_loss < max_val_loss:
            best_model = copy.deepcopy(model)
            max_val_loss = epoch_avg_val_loss

            torch.save(best_model.state_dict(), os.path.join(output_dir, f"{model_name}_bestmodel.pt"))
            print(f'Best Validation loss  : {epoch_avg_val_loss}')

        if epoch % myconfig.get('save_every') == 0 or epoch == epochs - 1:
            torch.save(
                model.state_dict(),
                os.path.join(output_dir, f"{model_name}-{epoch:03d}.pt"),
            )

    print('15'*8)
    print(avg_train_loss)
    print(avg_val_loss)
    print('15'*8)

    return model


# def score(ref, hypo):
#     """
#     ref, dictionary of reference sentences (id, sentence)
#     hypo, dictionary of hypothesis sentences (id, sentence)
#     score, dictionary of scores
#     """
#     scorers = [
#         (Bleu(4), ["Bleu_1", "Bleu_2", "Bleu_3", "Bleu_4"]),
#         (Meteor(), "METEOR"),
#         (Rouge(), "ROUGE_L"),
#         (Cider(), "CIDEr")
#     ]
#     final_scores = {}
#     for scorer, method in scorers:
#         score, scores = scorer.compute_score(ref, hypo)
#         if type(score) == list:
#             for m, s in zip(method, score):
#                 final_scores[m] = s
#         else:
#             final_scores[method] = score
#     return final_scores
#
#
# def evaluation_metrics(gen, gts):
#     gts = PTBTokenizer.tokenize(gts)
#     gen = PTBTokenizer.tokenize(gen)
#     print(score(gts, gen))



def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True


def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()


def get_rank():
    if not is_dist_avail_and_initialized():
        return 0
    return dist.get_rank()


def is_main_process():
    return get_rank() == 0


def save_on_master(*args, **kwargs):
    if is_main_process():
        torch.save(*args, **kwargs)


def setup_for_distributed(is_master):
    """
    This function disables printing when not in master process
    """
    import builtins as __builtin__
    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        force = kwargs.pop('force', False)
        if is_master or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def init_distributed_mode():
    # launched with torch.distributed.launch
    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
        rank = int(os.environ["RANK"])
        world_size = int(os.environ['WORLD_SIZE'])
        gpu = int(os.environ['LOCAL_RANK'])
    # launched with submitit on a slurm cluster
    elif 'SLURM_PROCID' in os.environ:
        rank = int(os.environ['SLURM_PROCID'])
        gpu = args.rank % torch.cuda.device_count()
    # launched naively with `python main_dino.py`
    # we manually add MASTER_ADDR and MASTER_PORT to env variables
    elif torch.cuda.is_available():
        print('Will run the code on one GPU.')
        rank, gpu, world_size = 0, 0, 1
        os.environ['MASTER_ADDR'] = '127.0.0.1'
        os.environ['MASTER_PORT'] = '29500'
    else:
        print('Does not support training without GPU.')
        sys.exit(1)

    dist.init_process_group(
        backend='nccl',
        init_method='env://',
        world_size=world_size,
        rank=rank,
    )

    torch.cuda.set_device(gpu)
    print('| distributed init (rank {}): {}'.format(rank, 'env://'), flush=True)
    dist.barrier()
    setup_for_distributed(rank == 0)
    return gpu, rank, world_size


def main():
    # Init Distributed Mode
    gpu, rank, world_size = init_distributed_mode()

    myconfig = {
        'epochs': 6,
        'batch_size': 1,
        'train_data': './data/visdial/clip_feat_ViT-B_32_train_vqa.pkl',
        'val_data': './data/visdial/clip_feat_ViT-B_32_val_vqa.pkl',
        'out_dir': './visdial_vqa',
        'save_every': 1,
        'prefix_length': 10,
        'prefix_length_clip': 10,
        'only_prefix': True,
        'mapping_type': 'transformer',
        'num_layers': 8,
        'is_rn': False,
        'normalize_prefix': False,
        'model_name': 'visdial_vqa_model',
        'weights_path': ''

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

    world_size = torch.cuda.device_count()
    print("GPU-devices : {} ".format(world_size))
    train(gpu ,model, train_dataset, val_dataset, myconfig, myconfig.get('out_dir'),myconfig.get('model_name'),world_size)

if __name__ == '__main__':
    main()
