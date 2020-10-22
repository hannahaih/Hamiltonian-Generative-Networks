"""Script to train the Hamiltonian Generative Network
"""
import argparse
import os
import yaml

import numpy as np
import time
import torch
import tqdm

from utilities.integrator import Integrator
from utilities.training_logger import TrainingLogger
from utilities.loader import load_hgn, get_online_dataloaders, get_offline_dataloaders
from utilities.losses import reconstruction_loss, kld_loss

def _avoid_overwriting(experiment_id):
    # This function throws an error if the given experiment data already exists in runs/
    logdir = os.path.join('runs', experiment_id)
    if os.path.exists(logdir):
        assert len(os.listdir(logdir)) == 0,\
            f'Experiment id {experiment_id} already exists in runs/. Remove it, change the name ' \
            f'in the yaml file.'

class hgn_trainer:
    def __init__(self, params, cpu=False, resume=False):
        """Instantiate and train the Hamiltonian Generative Network.

        Args:
            params (dict): Experiment parameters (see experiment_params folder).
        """

        self.params = params
        self.cpu = cpu
        self.resume= resume

        # Set device
        self.device = 'cpu'
        # "cuda:" + str(
            # params["gpu_id"]) if torch.cuda.is_available() else "cpu"

        # Get dtype, will raise a 'module 'torch' has no attribute' if there is a typo
        self.dtype = torch.__getattribute__(params["networks"]["dtype"])

        # Load hgn from parameters to deice
        self.hgn = load_hgn(params=self.params, device=self.device, dtype=self.dtype)

        # Either generate data on-the-fly or load the data from disk
        if "environment" in self.params:
            print("Training with ONLINE data...")
            self.train_data_loader, self.test_data_loader = get_online_dataloaders(self.params)
        else:
            print("Training with OFFLINE data...")
            self.train_data_loader, self.test_data_loader = get_offline_dataloaders(self.params)

        # Initialize training logger
        self.training_logger = TrainingLogger(hyper_params=self.params,
                                         loss_freq=1,
                                         rollout_freq=10,
                                         model_freq=10000)

        # Initialize tensorboard writer
        self.model_save_file = os.path.join(self.params["model_save_dir"], self.params["experiment_id"])

        # Define optimization modules
        optim_params = [
            {
                'params': self.hgn.encoder.parameters(),
                'lr': params["optimization"]["encoder_lr"]
            },
            {
                'params': self.hgn.transformer.parameters(),
                'lr': params["optimization"]["transformer_lr"]
            },
            {
                'params': self.hgn.hnn.parameters(),
                'lr': params["optimization"]["hnn_lr"]
            },
            {
                'params': self.hgn.decoder.parameters(),
                'lr': params["optimization"]["decoder_lr"]
            },
        ]
        self.optimizer = torch.optim.Adam(optim_params)

    def training_step(self, rollouts):
        """Perform a training step with the given rollouts batch.

        TODO: Move the whole fit() code inside forward if adding hooks to the training, as direct
            calls to self.forward() do not trigger them.

        Args:
            rollouts (torch.Tensor): Tensor of shape (batch_size, seq_len, channels, height, width)
                corresponding to a batch of sampled rollouts.
            variational (bool): Whether to sample from the encoder and compute the KL loss,
                or train in fully deterministic mode.

        Returns:
            A tuple (reconstruction_loss, kl_error, result) where reconstruction_loss and
            kl_error are floats and result is the HGNResult object with data of the forward pass.
        """
        # Re-set gradients and forward new batch
        prediction = self.hgn.forward(rollout_batch=rollouts)
        
        # Compute frame reconstruction error
        rec_loss = reconstruction_loss(target=prediction.input,
                                        prediction=prediction.reconstructed_rollout)
        train_loss = rec_loss

        kld = None
        langrange_multiplier = None
        if self.params["networks"]["variational"]:
            # Compute KL divergence
            mu = prediction.z_mean
            logvar = prediction.z_logvar
            kld = kld_loss(mu, logvar)
            # Compute loss
            langrange_multiplier =  1 # TODO(Stathi) Compute beta value
            train_loss += langrange_multiplier * kld

            if self.C_ma is None:
                pass
            else:
                pass
        
        losses = { 'train_loss': train_loss,
                   'reconstruction_loss': rec_loss,
                   'kld': kld,
                   'langrange_multiplier': langrange_multiplier
                   }
        return losses, prediction

    def fit(self):
        """
        The trainer fits an HGN.
        """
        self.C_ma = None
        # TRAIN
        for ep in range(params["optimization"]["epochs"]):
            print("Epoch %s / %s" %
                  (str(ep + 1), str(params["optimization"]["epochs"])))
            pbar = tqdm.tqdm(self.train_data_loader)
            for batch_idx, rollout_batch in enumerate(pbar):
                # Move to device and change dtype
                rollout_batch = rollout_batch.to(self.device).type(self.dtype)

                # Do an optimization step
                self.optimizer.zero_grad()
                losses, prediction = self.training_step(rollouts=rollout_batch)
                losses['train_loss'].backward()
                self.optimizer.step()

                # Log progress
                self.training_logger.step(losses=losses,
                                     rollout_batch=rollout_batch,
                                     prediction=prediction,
                                     model=self.hgn)

                # Progress-bar msg
                msg = ", ".join([f"{k}: {v:.5f}" for k,v in losses.items() if v is not None])
                pbar.set_description(msg)
            # Save model
            self.hgn.save(model_save_file)

        self.test()
        return self.hgn

    def test(self):
        """
        Test after the training is finished.
        """
        print("Testing...")
        test_error = 0
        pbar = tqdm.tqdm(self.test_data_loader)
        for _, rollout_batch in enumerate(pbar):
            rollout_batch = rollout_batch.to(self.device).type(self.dtype)
            prediction = self.hgn.forward(rollout_batch=rollout_batch)
            error = reconstruction_loss(target=prediction.input,
                prediction=prediction.reconstructed_rollout).detach().cpu().numpy()
            test_error += error / len(self.test_data_loader)
        self.training_logger.log_test_error(test_error)

if __name__ == "__main__":

    DEFAULT_PARAM_FILE = "experiment_params/default_online.yaml"

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--params', action='store', nargs=1, required=False,
        help='Path to the yaml file with the training configuration. If not specified,'
             'experiment_params/default_online.yaml will be used'
    )
    parser.add_argument(
        '--name', action='store', nargs=1, required=False,
        help='If specified, this name will be used instead of experiment_name of the yaml file.'
    )
    parser.add_argument(
        '--cpu', action='store_true', required=False, default=False,
        help='If specified, the training will be run on cpu. Otherwise, it will be run on GPU, '
             'unless GPU is not available.'
    )
    parser.add_argument(
        '--resume', action='store', required=False, nargs='?', default=None,
        help='Resume the training from a saved model. If a path is provided, the training will '
             'be resumed from the given checkpoint. Otherwise, the last checkpoint will be taken '
             'from saved_models/<experiment_id>'
    )
    args = parser.parse_args()

    if args.resume is not None:
        raise NotImplementedError('Resume training from command line is not implemented yet')

    params_file = args.params[0] if args.params is not None else DEFAULT_PARAM_FILE
    # Read parameters
    with open(params_file, 'r') as f:
        params = yaml.load(f, Loader=yaml.FullLoader)

    if args.name is not None:
        params['experiment_id'] = args.name[0]
    # Train HGN network
    trainer = hgn_trainer(params, cpu=args.cpu)
    hgn = trainer.fit()