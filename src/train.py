
import torch, math
import torch.nn as nn
from torch.nn import functional as F
from pathlib import Path
import matplotlib.pyplot as plt

import wandb
wandb.login()


#hyperparameters
batch_size= 32
block_size = 1024
max_iters = 1001
eval_iterval = 200
lr = 3e-4
device = 'mps' if torch.backends.mps.is_available() else 'cuda' if torch.cuda.is_available() else 'cpu'
eval_iters = 200
n_emb = 768
n_layer = 6
n_head = 6
dropout = 0.1

dtype = torch.long # 'bfloat16' # 'float32' or 'bfloat16'
#-------
torch.manual_seed(1337)


# dataset to train on. Let's download the tiny shakespeare dataset
#!wget https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt

with open('input.txt', 'r', encoding='utf-8') as f:
    text = f.read()


chars = sorted(list(set(text)))
vocab_size = len(chars)

stoi = {ch:i for i,ch in enumerate(chars)}
itos = {i:ch for i,ch in enumerate(chars)}
encode = lambda s: [stoi[c] for c in s] # encoder takes a string and outputs list of intergers
decoder = lambda l: ''.join([itos[i] for i in l]) # decoder takes intergers and outputs a strings

data = torch.tensor(encode(text), dtype = dtype)
# split our dataset in to train and validatioin set
n = int(0.9*len(text)) # the first 90% will be for training and 10% for validation
#-----------------------
# sampling a very small dataset for a sanity check and overfitting on a very small data 
#n_over, n_val= int(0.01*len(data)),int(0.005*len(data))
# train_data = data[:n_over]
# val_data = data[n_over:n_over+n_val]
#-----------------------
train_data = data[:n]
val_data = data[n:]

#dataloader
def get_batch(split):
    data = train_data if split=='train' else val_data
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([data[i:i+block_size] for i in ix])
    y = torch.stack([data[i+1:i+block_size+1] for i in ix])
    x, y = x.to(device), y.to(device)
    return x,y

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out


class Head(nn.Module):
    """ one head of self-attention """

    def __init__(self, head_size):
        super().__init__()

        #lets see single head perform self-attention
        self.key = nn.Linear(n_emb, head_size, bias=False)
        self.query = nn.Linear(n_emb, head_size, bias=False)
        self.value = nn.Linear(n_emb, head_size, bias=False)
        self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))

        self.dropout = nn.Dropout(dropout)

    def forward(self, x):

        B,T,C = x.shape
        k = self.key(x)  # (B, T, C)
        q = self.query(x) # (B, T, C)
        #scaled attention is for making wei unit varinace 
        wei = q @ k.transpose(-2, -1) * (C**0.5)# (B,T, C) @ (B, C,T) --> (B,T,T)
        wei = wei.masked_fill(self.tril[:T,:T]==0, float('-inf')) # B,T,T
        wei = F.softmax(wei, dim=-1) # B,T,T
        wei = self.dropout(wei)
        v = self.value(x)
        out = wei @ v
        return out

class Multi_head_attention(nn.Module):
    """ multiple heads of self attention in parallel"""

    def __init__(self, num_heads, head_size):
        super().__init__()
        self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
        self.proj = nn.Linear(n_emb, n_emb)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = torch.cat([h(x) for h in self.heads], dim=-1)
        out = self.dropout(self.proj(out))
        return out
    
# aka MLP model
class FeedForward(nn.Module):
    """ simple linear layer followed by non-linearity """

    def __init__(self, n_emb):
        super().__init__()
        self.net = nn.Sequential(
                nn.Linear(n_emb, 4 *n_emb),
                nn.ReLU(),
                nn.Linear(4 * n_emb, n_emb),
                nn.Dropout(dropout),
        )

    def forward(self,x):
        return self.net(x)

class Block(nn.Module):
    """ Transformer block: communication followed by computation """

    def __init__(self,n_emb, n_head):
        super().__init__()
        head_size = n_emb//n_head
        self.sa = Multi_head_attention(n_head, head_size)
        self.ffwd = FeedForward(n_emb)
        self.ln1 = nn.LayerNorm(n_emb)
        self.ln2 = nn.LayerNorm(n_emb)

    def forward(self, x):
        x = x + self.sa(self.ln1(x))
        x = x + self.ffwd(self.ln2(x))
        return x


class NanoGptModel(nn.Module):
    #def __init__(self, vocab_size):
    def __init__(self):
        super().__init__()
        self.token_embedding_table = nn.Embedding(vocab_size,n_emb)
        self.possitinal_embedding_table = nn.Embedding(block_size, n_emb)
        self.blocks = nn.Sequential(*[Block(n_emb, n_head= n_head)for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_emb)
        self.lm_head = nn.Linear(n_emb, vocab_size) 
        

    def forward(self,idx, targets=None): 
        B , T = idx.shape

        #idx, and tagets are both in dimention (B,T) tensor of integers
        tok_emb = self.token_embedding_table(idx) # B,T, C, Batch, T (block_size), C(n_embed C)
        posi_emb = self.possitinal_embedding_table(torch.arange(T, device = device)) # (T, C)
        x = tok_emb + posi_emb # B,T,C
        x = self.blocks(x) #(B,T,C)
        logits = self.lm_head(x) # B, T, C(vocab_size C)

        if targets is None:
            loss = None
        else:
            B, T, C = logits.shape # pytorch expects the dimentions to be in B, C, T so lets reshape
            logits = logits.view(B*T, C)
            targets = targets.view(B*T)# we can also use -1 and pytorch will sort out but to be explicit
            loss = F.cross_entropy(logits, targets)
        return logits,loss
    
    def generate(self, idx, max_tokens):
        for _ in range(max_tokens):
            #cropping the context ( idx) to the last block_size tokens, otherwise our posi_emb will run out of scope
            idx_cond = idx[:, -block_size:]
            #getting the predictios
            logits,loss = self(idx_cond)
            #focus on the last of time step
            logits = logits[:,-1,:] # becomes (B, C)
            probs = F.softmax(logits, dim=-1) # (B, C)
            # sampling from distribution
            id_next = torch.multinomial(probs, num_samples=1)# (B, C)
            idx = torch.cat((idx, id_next), dim = 1) # (B, T+1)
        return idx


# wandb tracking initialization
wandb.init(project = 'nano-gpt-tracking-test',
      config={
            "batch_size" :32,
            "block_size" : 1024,
            "max_iters" :1001,
            "eval_iterval" : 200,
            "lr" : 3e-4,
            "eval_iters" : 200,
            "n_emb" : 768,
            "n_layer" : 6,
            "n_head" :6,
            "dropout" : 0.1,
            #"dtype" : 'bfloat16' didnt work yet need some debugging,
}
)

model = NanoGptModel().to(device)

#torch compile
m = torch.compile(model)

#wandb watch
wandb.watch(m)

print(sum(p.numel() for p in m.parameters())/1e6, 'M parameters')

optimizer = torch.optim.AdamW(model.parameters(), lr= lr)
lossi = []
for iter in range(max_iters): 
    if iter % eval_iterval == 0:
        losses = estimate_loss()
        print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
        
        #wandb log
        wandb.log({"steps": iter,"train_loss": losses["train"], "val_loss": losses["val"]})

    xb,yb = get_batch('train')
    logits, loss = model(xb, yb)
    optimizer.zero_grad(set_to_none=True)
    loss.backward()
    lr =  lr if max_iters > 20000 else lr*10    #step learing rate Decay 
    optimizer.step()

    # Free up unoccupied cached memory
    torch.cuda.empty_cache()


context = torch.zeros((1,1), dtype=dtype, device= device)
print(decoder(m.generate(context, max_tokens=500)[0].tolist()))

# mdl_path = Path('models')
# mdl_path.mkdir(exist_ok=True)
# torch.save(model, mdl_path/'nanoGpt_12.8M_para.pkl')

