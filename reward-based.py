import copy
import glob
import os
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
import random

from accelerate import Accelerator
from datasets import Dataset
import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.optim import AdamW
from tqdm.auto import tqdm
from transformers import get_scheduler

from config import GenericArgs, InfillArgs, WatermarkArgs
from utils.infill_config import Policy, Agent
from utils.infill_utils import featurize_for_masking_ours, featurize_for_masking_random, tokenize_function, collator_for_loading_pkl
from utils.logging import getLogger

# print(torch.cuda.is_available())
# exit()
random.seed(1230)

# @record
def main():
    infill_parser = InfillArgs()
    generic_parser = GenericArgs()
    wm_parser = WatermarkArgs()
    infill_args, _ = infill_parser.parse_known_args()
    generic_args, _ = generic_parser.parse_known_args()
    wm_args, _ = wm_parser.parse_known_args()
    DEBUG_MODE = generic_args.debug_mode
    dtype = generic_args.dtype

    dirname = f'./logs/train-infill/{dtype}/{generic_args.exp_name}'
    logger = getLogger("TRAIN-RL",
                       dir_=dirname,
                       debug_mode=DEBUG_MODE)

    _DATADIR = f"./data/train_infill/cache/{dtype}/{generic_args.exp_name}"
    if not os.path.exists(_DATADIR):
        os.makedirs(_DATADIR)

    logger.info(f"Infill Args: \n {infill_args}")

    PREPROCESS_DATA = True
    if PREPROCESS_DATA:
        augmented_data_path = f"./data/{dtype}-augmented.txt"
        clean_text = []
        corrupted_text = []

        with open(augmented_data_path, "r", encoding="utf-8") as reader:
            for line in reader:
                line = line.split("[sep]")
                for idx in range(len(line)-1):
                    clean_text.append(line[0])
                    corrupted_text.append(line[idx+1])

        # shuffle the instances with a fixed seed so that the clean-corrupted pairs are maintained
        random.Random(0).shuffle(clean_text)
        random.Random(0).shuffle(corrupted_text)

        tokenizer = INFILL_TOKENIZER

        batch = clean_text
        corr_batch = corrupted_text

        clean_dataset = Dataset.from_dict({"text": batch})
        corr_dataset = Dataset.from_dict({"text": corr_batch})

        feature = clean_dataset.map(tokenize_function, batched=True)
        corr_feature = corr_dataset.map(tokenize_function, batched=True)

        feature = feature.add_column("corr_input_ids", corr_feature['input_ids'])
        feature = feature.add_column("corr_attention_mask", corr_feature['attention_mask'])

        mask_kwargs = {'method': wm_args.mask_select_method,
                       "mask_order_by": wm_args.mask_order_by,
                       "keyword_mask": wm_args.keyword_mask,
                       'exclude_cc': wm_args.exclude_cc
                       }
        logger.info(f"Masking Options: \n {mask_kwargs}")
        # mask_selector = MaskSelector(**mask_kwargs)
        # keyword_module = KeywordExtractor(ratio=wm_args.keyword_ratio)

        # train model
        pt_dataset = feature.train_test_split(
            train_size=0.6,
            test_size=0.4,
            shuffle=False
        )
        eval_dataset = pt_dataset['test']
        train_bs = 64 if not DEBUG_MODE else 8
        train_dataset = pt_dataset['train']


        logger.info("Processing train data...")
        # last_idx = len(train_dataset) // train_bs + 1
        # progress_bar = tqdm(range(last_idx))
        # for b_idx in range(last_idx):
        #     batch = train_dataset[b_idx*train_bs: (b_idx+1)*train_bs]
        #     if len(batch):
        #         batch = pd.DataFrame(batch).to_dict(orient="records")
        #         save_dir = os.path.join(_DATADIR, f"train-{b_idx}.pkl")
        #         if infill_args.masking_type == "ours":
        #             featurize_for_masking_ours(batch, mask_selector, keyword_module, save_dir)
        #         else:
        #             featurize_for_masking_random(batch, infill_args.masking_p, save_dir)
        #         progress_bar.update(1)
        #     if DEBUG_MODE and b_idx == 1:
        #         break
        #
        # logger.info("Processing eval. data...")
        # last_idx = len(eval_dataset) // train_bs + 1
        # progress_bar = tqdm(range(last_idx))
        # for b_idx in range(last_idx):
        #     batch = eval_dataset[b_idx*train_bs: (b_idx+1)*train_bs]
        #     if len(batch):
        #         batch = pd.DataFrame(batch).to_dict(orient="records")
        #         save_dir = os.path.join(_DATADIR, f"eval-{b_idx}.pkl")
        #         if infill_args.masking_type == "ours":
        #             featurize_for_masking_ours(batch, mask_selector, keyword_module, save_dir)
        #         else:
        #             featurize_for_masking_random(batch, infill_args.masking_p, save_dir)
        #         progress_bar.update(1)
        #     if DEBUG_MODE and b_idx == 1:
        #         break

    # if len(glob.glob(os.path.join(_DATADIR, "*.pkl"))) == 0:
    #     logger.info(f"Data does not exit in {_DATADIR}. Ending process")
    #     exit()

    # train_paths = glob.glob(os.path.join(_DATADIR, "train*.pkl"))
    # eval_paths = glob.glob(os.path.join(_DATADIR, "eval*.pkl"))
    train_dl = DataLoader(
        train_dataset,
        shuffle=True,
        batch_size=train_bs,
    )
    eval_dl = DataLoader(
        eval_dataset,
        shuffle=False,
        batch_size=train_bs
    )
    print(next(iter(train_dl)))
    exit()
    policy = Policy()
    agent = Agent()

    params = [p for n, p in model.named_parameters()]
    optimizer = AdamW(params, lr=5e-5)
    fixed_model = copy.deepcopy(model)
    fixed_model.eval()

    num_train_epochs = infill_args.num_epochs
    num_update_steps_per_epoch = len(train_dl)
    num_training_steps = num_train_epochs * num_update_steps_per_epoch

    lr_scheduler = get_scheduler(
        "linear",
        optimizer=optimizer,
        num_warmup_steps=0.1,
        num_training_steps=num_training_steps,
    )

    accelerator = Accelerator()
    # load from checkpoint
    if infill_args.model_ckpt:
        model.from_pretrained(infill_args.model_ckpt)
        optim_scheduler_states = torch.load(os.path.join(infill_args.model_ckpt, "/optim_state.pth"))

        logger.info("Loading optimizer states from checkpoint dir ..")
        optimizer.load_state_dict(optim_scheduler_states["optimizer"])
        completed_epochs = optim_scheduler_states["epoch"]
        completed_steps = optim_scheduler_states["steps"]
        lr_scheduler.load_state_dict(optim_scheduler_states["scheduler"])

    model, fixed_model, optimizer, train_dl, eval_dl = accelerator.prepare(
        model, fixed_model, optimizer, train_dl, eval_dl
    )

    kl_criterion = torch.nn.KLDivLoss(reduction="batchmean")
    eval_freq = 20000
    log_freq = 1000
    kl_weight = 1.0
    topk = 32
    optimize_topk = infill_args.optimize_topk
    use_logit_loss = False
    optimize_cls_token = False
    mse_criterion = torch.nn.MSELoss()
    logit_loss_w = 1.0
    kl_type = infill_args.kl_type

    ckpt_dir = f"./ckpt/{dtype}/{generic_args.exp_name}/"
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)




    step = 0
    progress_bar = tqdm(range(num_training_steps))

    for epoch in range(num_train_epochs):
        # Train metric
        tr_losses = {"mlm": [], "r_mlm": [], "acc": [], "ll": []}

        for b_idx, (batch, corr_batch) in enumerate(train_dl):
            model.train()
            with torch.no_grad():
                outputs = fixed_model(**batch)
                if optimize_cls_token:
                    masked_index = torch.logical_or(batch['input_ids'] == tokenizer.mask_token_id,
                                                batch['input_ids'] == 101).nonzero(as_tuple=True)
                else:
                    masked_index = (batch['input_ids'] == tokenizer.mask_token_id).nonzero(as_tuple=True)

            corr_outputs = model(**corr_batch)
            if optimize_cls_token:
                corr_masked_index = torch.logical_or(corr_batch['input_ids'] == tokenizer.mask_token_id,
                                                 corr_batch['input_ids'] == 101).nonzero(as_tuple=True)
            else:
                corr_masked_index = (corr_batch['input_ids'] == tokenizer.mask_token_id).nonzero(as_tuple=True)

            ppl_loss = corr_outputs.loss
            # the target distribution is detached from graph
            target_dist = F.softmax(outputs.logits[masked_index], dim=-1)
            pred_dist = F.softmax(corr_outputs.logits[corr_masked_index], dim=-1)

            if target_dist.shape[0] != pred_dist.shape[0]:
                logger.info(
                    f"Number of masked tokens different for {b_idx} : target {target_dist.shape[0]} , pred: {pred_dist.shape[0]}")
                breakpoint()

            kl_loss, logit_loss, acc = compute_loss(target_dist, pred_dist, kl_criterion,
                                                    outputs.logits[masked_index], corr_outputs.logits[corr_masked_index],
                                                    mse_criterion=mse_criterion,
                                                    optimize_topk=optimize_topk,
                                                    use_logit_loss=use_logit_loss,
                                                    kl_type=kl_type)
            if kl_loss == float("inf") or kl_loss == float("-inf"):
                logger.info("KL loss is inf!")
                breakpoint()
            loss = kl_loss + logit_loss * logit_loss_w
            accelerator.backward(loss)

            optimizer.step()
            lr_scheduler.step()
            optimizer.zero_grad()
            progress_bar.update(1)
            step += 1

            bs = batch['labels'].shape[0]
            tr_losses['mlm'].append(accelerator.gather(ppl_loss.detach().repeat(bs)))
            tr_losses['r_mlm'].append(accelerator.gather(kl_loss.detach().repeat(bs)))
            if len(acc):
                tr_losses['acc'].append(acc)
            tr_losses['ll'].append(accelerator.gather(logit_loss.detach().repeat(bs)))

            if step % log_freq == 0:
                log_output = ""
                for k, v in tr_losses.items():
                    if len(v):
                        mean_loss = torch.cat(v).mean()
                        log_output += f"{k}: {mean_loss:.3f}\t"
                        tr_losses[k] = []
                logger.info(f">>>Train log at Epoch {epoch}, Step {step}/{num_training_steps}\t"
                            f"{log_output}")

            if step % eval_freq == 0 or step == num_training_steps:
                # Evaluation
                evaluate(eval_dl, epoch, step, save_ckpt=True)

    accelerator.wait_for_everyone()
    unwrapped = accelerator.unwrap_model(model)
    unwrapped.save_pretrained(
        os.path.join(ckpt_dir, f"last")
    )
    accelerator.save(
        {
            "epoch": epoch,
            "steps": step,
            "optimizer": optimizer.state_dict(),
            "scheduler": lr_scheduler.state_dict()
        },
        os.path.join(ckpt_dir, "last/optim_state.pth")
    )

if __name__ == "__main__":
    main()