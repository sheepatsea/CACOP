from models.geco import build_model
from models.matcher import build_matcher
from torchvision import ops
from utils.data import FSC147Dataset
from utils.arg_parser import get_argparser
from utils.losses import SetCriterion
from time import perf_counter
import argparse
import os
import torch
from torch import nn
from torch.utils.data import DataLoader, DistributedSampler
from torch.nn.parallel import DistributedDataParallel
from torch import distributed as dist
from utils.data import pad_collate
import numpy as np
import random

torch.manual_seed(0)
random.seed(0)
np.random.seed(0)

DATASETS = {
    'fsc147': FSC147Dataset
}

def train(args):
    if 'SLURM_PROCID' in os.environ:
        world_size = int(os.environ['SLURM_NTASKS'])
        rank = int(os.environ['SLURM_PROCID'])
        gpu = rank % torch.cuda.device_count()
        print("Running on SLURM", world_size, rank, gpu)
    else:
        world_size = int(os.environ['WORLD_SIZE'])
        rank = int(os.environ['RANK'])
        gpu = int(os.environ['LOCAL_RANK'])

    torch.cuda.set_device(gpu)
    device = torch.device(gpu)

    dist.init_process_group(
        backend='nccl', init_method='env://',
        world_size=world_size, rank=rank
    )

    model = DistributedDataParallel(
        build_model(args).to(device),
        device_ids=[gpu],
        output_device=gpu
    )

    backbone_params = dict()
    non_backbone_params = dict()
    for n, p in model.named_parameters():
        if 'backbone' in n:
            backbone_params[n] = p
        else:
            non_backbone_params[n] = p

    optimizer = torch.optim.AdamW(
        [
            {'params': non_backbone_params.values()},
            {'params': backbone_params.values(), 'lr': args.backbone_lr}
        ],
        lr=args.lr,
        weight_decay=args.weight_decay,
    )
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop, gamma=0.25)
    if args.resume_training:
        checkpoint = torch.load(os.path.join(args.model_path, f'{args.model_name}.pth'))
        model.load_state_dict(checkpoint['model'], strict=False)

    start_epoch = 0
    best = 10000000000000
    matcher = build_matcher(args)
    criterion = SetCriterion(0, matcher, {"loss_giou": args.giou_loss_coef}, ["bboxes", "ce"],
                             focal_alpha=args.focal_alpha)
    criterion.to(device)

    train = DATASETS[args.dataset](
        args.data_path,
        args.image_size,
        split='train',
        num_objects=args.num_objects,
        tiling_p=args.tiling_p,
        zero_shot=args.zero_shot
    )
    val = DATASETS[args.dataset](
        args.data_path,
        args.image_size,
        split='val',
        num_objects=args.num_objects,
        tiling_p=args.tiling_p
    )
    train_loader = DataLoader(
        train,
        sampler=DistributedSampler(train),
        batch_size=args.batch_size,
        drop_last=True,
        num_workers=args.num_workers,
        collate_fn=pad_collate
    )
    val_loader = DataLoader(
        val,
        sampler=DistributedSampler(val),
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.num_workers,
        collate_fn=pad_collate
    )


    print(rank)
    for epoch in range(start_epoch + 1, args.epochs + 1):
        if rank == 0:
            start = perf_counter()
        train_loss = torch.tensor(0.0).to(device)
        val_loss = torch.tensor(0.0).to(device)
        train_ae = torch.tensor(0.0).to(device)
        val_ae = torch.tensor(0.0).to(device)
        val_rmse = torch.tensor(0.0).to(device)

        train_loader.sampler.set_epoch(epoch)
        model.train()
        criterion.train()
        for img, bboxes, img_name, gt_bboxes, _ in train_loader:
            img = img.to(device)
            bboxes = bboxes.to(device)
            gt_bboxes = gt_bboxes.to(device)

            optimizer.zero_grad()
            outputs, ref_points, centerness, outputs_coord = model(img, bboxes)

            losses = []
            num_objects_gt = []
            num_objects_pred = []

            nms_bboxes = []
            for idx in range(img.shape[0]):
                target_bboxes = gt_bboxes[idx][torch.logical_not((gt_bboxes[idx] == 0).all(dim=1))] / 1024

                l = criterion(outputs[idx],
                              [{"boxes": target_bboxes, "labels": torch.tensor([0] * target_bboxes.shape[0])}],
                              centerness[idx], ref_points[idx])
                keep = ops.nms(outputs[idx]['pred_boxes'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8],
                               outputs[idx]['box_v'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8], 0.5)

                num_objects_gt.append(len(target_bboxes))

                boxes = (outputs[idx]['pred_boxes'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8])[keep]
                nms_bboxes.append(boxes)
                num_objects_pred.append(len(boxes))
                losses.append(l['loss_giou'] + l["loss_l2"] + + l["loss_bbox"])
            loss = sum(losses)

            loss.backward()

            if args.max_grad_norm > 0:
                nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
            optimizer.step()
            train_loss += loss
            train_ae += torch.abs(torch.tensor(num_objects_gt) - torch.tensor(num_objects_pred)).sum()
        criterion.eval()
        model.eval()
        with torch.no_grad():
            for img, bboxes, img_name, gt_bboxes, _ in val_loader:
                img = img.to(device)
                bboxes = bboxes.to(device)
                gt_bboxes = gt_bboxes.to(device)

                optimizer.zero_grad()
                outputs, ref_points, centerness, outputs_coord = model(img, bboxes)

                losses = []
                num_objects_gt = []
                num_objects_pred = []
                nms_bboxes = []

                for idx in range(img.shape[0]):
                    # print(img_name[idx])
                    target_bboxes = gt_bboxes[idx][torch.logical_not((gt_bboxes[idx] == 0).all(dim=1))] / 1024

                    l = criterion(outputs[idx],
                                  [{"boxes": target_bboxes, "labels": torch.tensor([0] * target_bboxes.shape[0])}],
                                  centerness[idx], ref_points[idx])
                    keep = ops.nms(outputs[idx]['pred_boxes'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8],
                                   outputs[idx]['box_v'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8], 0.5)

                    num_objects_gt.append(len(target_bboxes))

                    boxes = (outputs[idx]['pred_boxes'][outputs[idx]['box_v'] > outputs[idx]['box_v'].max() / 8])[keep]
                    nms_bboxes.append(boxes)
                    num_objects_pred.append(len(boxes))
                    losses.append(l['loss_giou'] + l["loss_l2"] + l["loss_bbox"])
                loss = sum(losses)

                train_loss += loss
                num_objects_gt = torch.tensor(num_objects_gt)
                num_objects_pred = torch.tensor(num_objects_pred)

                val_loss += loss
                val_ae += torch.abs(
                    num_objects_gt - num_objects_pred
                ).sum()
                val_rmse += torch.pow(
                    num_objects_gt - num_objects_pred, 2
                ).sum()

                if args.max_grad_norm > 0:
                    nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()

        dist.all_reduce(train_loss)
        dist.all_reduce(val_loss)
        dist.all_reduce(val_rmse)
        dist.all_reduce(train_ae)
        dist.all_reduce(val_ae)

        scheduler.step()

        if rank == 0:
            end = perf_counter()
            best_epoch = False
            if val_rmse.item() / len(val) < best:
                best = val_rmse.item() / len(val)
                checkpoint = {
                    'epoch': epoch,
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'scheduler': scheduler.state_dict(),
                    'best_val_ae': val_rmse.item() / len(val)
                }

                torch.save(
                    checkpoint,
                    os.path.join(args.model_path, f'{args.model_name_resumed}.pth')
                )

                best_epoch = True

            print(
                f"Epoch: {epoch}",
                f"Train loss: {train_loss.item():.3f}",
                f"Val loss: {val_loss.item():.3f}",
                f"Train MAE: {train_ae.item() / len(train):.3f}",
                f"Val MAE: {val_ae.item() / len(val):.3f}",
                f"Val RMSE: {torch.sqrt(val_rmse / len(val)).item():.2f}",
                f"Epoch time: {end - start:.3f} seconds",
                'best' if best_epoch else ''
            )

    dist.destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser('GeCo', parents=[get_argparser()])
    args = parser.parse_args()
    print(args)
    train(args)