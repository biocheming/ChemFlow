import torch
import lightning as L
from lightning.pytorch import LightningDataModule
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.callbacks import LearningRateMonitor, ModelCheckpoint
from lightning.pytorch.cli import LightningArgumentParser

from torch.utils.data import DataLoader, TensorDataset, random_split

from pathlib import Path

from cd2root import cd2root

cd2root()

from src.predictor import Predictor
from src.vae import VAE, load_vae
from src.pinn import PropGenerator
from src.pinn.pde import WavePDEModel
from src.utils.scores import *


class DataModule(LightningDataModule):
    def __init__(self, vae: VAE = None, n: int = 100_000, batch_size: int = 100):
        super().__init__()

        self.save_hyperparameters(ignore="vae")

        self.n = n
        self.batch_size = batch_size

        self.dataset = TensorDataset(
            torch.randn(n, vae.latent_dim),
        )
        self.train_data, self.test_data = random_split(
            self.dataset, [0.9, 0.1], generator=torch.Generator().manual_seed(42)
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_data, batch_size=self.batch_size, shuffle=True, drop_last=True
        )

    def val_dataloader(self):
        return DataLoader(self.test_data, batch_size=self.batch_size)


def parse_args():
    parser = LightningArgumentParser()

    parser.add_argument("-s", "--seed", type=int, default=42)
    parser.add_argument("-e", "--epochs", type=int, default=30)
    parser.add_argument("-o", "--output", type=str, default="checkpoints")
    parser.add_argument("-p", "--prop", type=str, default="qed")

    parser.add_lightning_class_args(WavePDEModel, "model")
    parser.add_lightning_class_args(DataModule, "data")

    args = parser.parse_args()

    del args.model.generator, args.data.vae, args.model.k, args.model.minimize_jvp

    return args


def main():
    args = parse_args()

    L.seed_everything(args.seed)

    output_path = (
        Path(args.output) / f"{args.model.pde_function}pde_prop" / "zmc" / args.prop
    )
    output_path.mkdir(parents=True, exist_ok=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    dm, vae = load_vae(
        file_path="data/processed/zmc.smi",
        model_path="checkpoints/vae/zmc/checkpoint.pt",
        device=device,
    )

    datamodel = DataModule(vae=vae, **args.data)

    predictor = Predictor(vae.max_len * vae.vocab_size).to(device)
    predictor.load_state_dict(
        torch.load(f"checkpoints/prop_predictor/{args.prop}/checkpoint.pt")
    )
    predictor.eval()
    for p in predictor.parameters():
        p.requires_grad = False

    generator = PropGenerator(vae, predictor).to(device)
    model = WavePDEModel(
        generator=generator,
        k=1,
        minimize_jvp=args.prop in MINIMIZE_PROPS or args.prop in PROTEIN_FILES,
        **args.model,
    ).to(device)

    trainer = L.Trainer(
        # gpus=1,
        # accelerator="gpu",
        # devices=1,
        max_epochs=args.epochs,
        max_steps=100_000,
        # logger=L.loggers.CSVLogger("logs"),
        logger=[
            WandbLogger(
                project=f"soc_{model.pde.pde_function}pde_prop",
                entity="soc_mol",
                name=f"{args.prop}_norm_{model.pde.normalize}",
            )
        ],
        callbacks=[
            LearningRateMonitor(logging_interval="epoch"),
            ModelCheckpoint(
                monitor="val/loss",
                mode="min",
                save_top_k=1,
                dirpath=output_path,
                save_last=True,
            ),
        ],
        # enable_checkpointing=False,
        # detect_anomaly=True,
    )
    print("Training..")
    trainer.fit(
        model,
        datamodel,
        # ckpt_path=output_path / "last.ckpt"
    )

    # load best checkpoint
    model = WavePDEModel.load_from_checkpoint(
        trainer.checkpoint_callback.best_model_path, generator=generator, **args.model
    ).to(device)
    wavepde = model.pde

    # trainer.validate(model, datamodel)

    print("Saving..")
    torch.save(wavepde.state_dict(), output_path / "checkpoint.pt")


if __name__ == "__main__":
    main()
