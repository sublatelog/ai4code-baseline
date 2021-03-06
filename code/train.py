import json
from pathlib import Path
from dataset import *
import numpy as np
import pandas as pd
from torch.utils.data import DataLoader, Dataset
from model import *
from tqdm import tqdm
import sys, os
from metrics import *
import torch
import argparse

import wandb
wandb.init(project="ai4code_baseline", entity="sublate")

parser = argparse.ArgumentParser(description='Process some arguments')
parser.add_argument('--model_name_or_path', type=str, default='microsoft/codebert-base')
parser.add_argument('--train_mark_path', type=str, default='./data/train_df_mark.csv')
parser.add_argument('--train_features_path', type=str, default='./data/train_fts.json')
parser.add_argument('--val_mark_path', type=str, default='./data/val_df_mark.csv')
parser.add_argument('--val_features_path', type=str, default='./data/val_fts.json')
parser.add_argument('--val_path', type=str, default="./data/val_df.csv")

parser.add_argument('--md_max_len', type=int, default=64)
parser.add_argument('--total_max_len', type=int, default=512)
parser.add_argument('--batch_size', type=int, default=8)
parser.add_argument('--accumulation_steps', type=int, default=4)
parser.add_argument('--epochs', type=int, default=5)
parser.add_argument('--n_workers', type=int, default=8)

parser.add_argument('--load_model', action='store_true')

args = parser.parse_args()

# if not os.path.isdir("./outputs"):
#     os.mkdir("./outputs")
    
data_dir = Path('/content/data')

train_df_mark = pd.read_csv(args.train_mark_path).drop("parent_id", axis=1).dropna().reset_index(drop=True)
train_fts = json.load(open(args.train_features_path))
val_df_mark = pd.read_csv(args.val_mark_path).drop("parent_id", axis=1).dropna().reset_index(drop=True)
val_fts = json.load(open(args.val_features_path))
val_df = pd.read_csv(args.val_path)

order_df = pd.read_csv("/content/data/train_orders.csv").set_index("id")
df_orders = pd.read_csv(
    data_dir / 'train_orders.csv',
    index_col='id',
    squeeze=True,
).str.split()

train_ds = MarkdownDataset(train_df_mark, model_name_or_path=args.model_name_or_path, md_max_len=args.md_max_len,
                           total_max_len=args.total_max_len, fts=train_fts)
val_ds = MarkdownDataset(val_df_mark, model_name_or_path=args.model_name_or_path, md_max_len=args.md_max_len,
                         total_max_len=args.total_max_len, fts=val_fts)
train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=args.n_workers,
                          pin_memory=False, drop_last=True)
val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False, num_workers=args.n_workers,
                        pin_memory=False, drop_last=False)


def read_data(data):
    return tuple(d.cuda() for d in data[:-1]), data[-1].cuda()


def validate(model, val_loader):
    model.eval()

    tbar = tqdm(val_loader, file=sys.stdout)

    preds = []
    labels = []

    with torch.no_grad():
        for idx, data in enumerate(tbar):
            inputs, target = read_data(data)

            with torch.cuda.amp.autocast():
                pred = model(*inputs)

            preds.append(pred.detach().cpu().numpy().ravel())
            labels.append(target.detach().cpu().numpy().ravel())

    return np.concatenate(labels), np.concatenate(preds)


def train(model, train_loader, val_loader, wandb, epochs):
    np.random.seed(0)
    # Creating optimizer and lr schedulers
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    num_train_optimization_steps = int(args.epochs * len(train_loader) / args.accumulation_steps)
    optimizer = AdamW(optimizer_grouped_parameters, 
                      lr=9e-5, # 3e-5, # 1e-5, #15e-5, # 
                      correct_bias=False
                     )  # To reproduce BertAdam specific behavior set correct_bias=False
#     scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0.01 * num_train_optimization_steps,
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=0.05 * num_train_optimization_steps,
                                                num_training_steps=num_train_optimization_steps)  # PyTorch scheduler

    criterion = torch.nn.L1Loss()
    scaler = torch.cuda.amp.GradScaler()
    
    
    # load checkpoint   
    epoch = 0    
    save_path = "/content/drive/MyDrive/kaggle/AI4Code/output/CodeBERT_training_state.pt"
    
    if args.load_model == True:             
        checkpoint = torch.load(save_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.cuda() # .to(device)
        epoch = checkpoint['epoch']
        loss = checkpoint['loss']
    

    
    for e in range(epoch, epochs):
        model.train()
        tbar = tqdm(train_loader, file=sys.stdout)
        loss_list = []
        preds = []
        labels = []

        for idx, data in enumerate(tbar):
            inputs, target = read_data(data)

            with torch.cuda.amp.autocast():
                pred = model(*inputs)
                loss = criterion(pred, target)
            scaler.scale(loss).backward()
            if idx % args.accumulation_steps == 0 or idx == len(tbar) - 1:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                scheduler.step()

            loss_list.append(loss.detach().cpu().item())
            preds.append(pred.detach().cpu().numpy().ravel())
            labels.append(target.detach().cpu().numpy().ravel())

            avg_loss = np.round(np.mean(loss_list), 4)

            tbar.set_description(f"Epoch {e + 1} Loss: {avg_loss} lr: {scheduler.get_last_lr()}")
            

        y_val, y_pred = validate(model, val_loader)
        val_df["pred"] = val_df.groupby(["id", "cell_type"])["rank"].rank(pct=True)
        
        if len(y_pred) == val_df.loc[val_df["cell_type"] == "markdown", "pred"].shape[0]:
            
            print("y_pred:",len(y_pred))
            print("val_df:",val_df.loc[val_df["cell_type"] == "markdown", "pred"].shape)

            val_df.loc[val_df["cell_type"] == "markdown", "pred"] = y_pred
            y_dummy = val_df.sort_values("pred").groupby('id')['cell_id'].apply(list)
            print("Preds score", kendall_tau(df_orders.loc[y_dummy.index], y_dummy))
            score = kendall_tau(df_orders.loc[y_dummy.index], y_dummy)
            
        else:
            print("y_pred:",len(y_pred))
            print("val_df:",val_df.loc[val_df["cell_type"] == "markdown", "pred"].shape)
            score = 0
        
        # wandb
        wandb.log({'Loss': avg_loss,
                   'lr': scheduler.get_last_lr(),          
                   'score': score,           
                  })
        
        
        # save
        # torch.save(model.state_dict(), "/content/drive/MyDrive/kaggle/AI4Code/output/model.bin")

        # ??????        
        torch.save({'epoch': e,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'loss': avg_loss,
                    'scaler': scaler.state_dict(),
                    'scheduler': scheduler.state_dict(),
                   },
                   save_path)

    return model, y_pred


model = MarkdownModel(args.model_name_or_path)
model = model.cuda()
model, y_pred = train(model, train_loader, val_loader, wandb, epochs=args.epochs)
