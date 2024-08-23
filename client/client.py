import os
from os import path
import sys

sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))
from transformers import TrainingArguments
from trl import SFTTrainer
from peft import set_peft_model_state_dict, get_peft_model_state_dict
from collections import OrderedDict
from datasets import load_dataset
from omegaconf import OmegaConf
from utils.process_data import apply_chat_template
from utils.models import get_model_and_tokenizer
from utils.differential_privacy import clip_l2_norm, local_add_gaussian_noise
from utils.chain_record import send_weight
import math
import copy
import torch
import socket
import pickle
import numpy as np


def cosine_lr(
    current_round: int,
    total_round: int,
    learning_rate_max: float = 0.001,
    learning_rate_min: float = 0.0,
) -> float:
    """Implement cosine learning rate."""
    cos_inner = math.pi * current_round / total_round
    return learning_rate_min + 0.5 * (learning_rate_max - learning_rate_min) * (
        1 + math.cos(cos_inner)
    )


class BaseClient:
    def __init__(self, client_id, cfg_path):
        self.cfg_path = cfg_path
        self.config_detail = OmegaConf.load(cfg_path)
        self.model = None
        self.tokenizer = None
        self.client_id = client_id
        self.training_args = TrainingArguments(
            **self.config_detail.sft.training_arguments
        )
        self.train_dataset = None
        self.test_dataset = None
        self.host = self.config_detail.client.host
        self.port = self.config_detail.client.port
        self.use_chain = self.config_detail.chain_record
        self.ldp = self.config_detail.client.local_dp

    def prepare_dataset(self):
        trainset_full = load_dataset(self.config_detail.datasetname, split="train")
        train_dataset = trainset_full["train"]
        test_dataset = trainset_full["test"]
        column_names = list(train_dataset.features)
        self.train_dataset = train_dataset.map(
            apply_chat_template,
            fn_kwargs={"tokenizer": self.tokenizer},
            num_proc=10,
            remove_columns=column_names,
            desc="Applying chat template to train_sft",
        )

        self.test_dataset = test_dataset.map(
            apply_chat_template,
            fn_kwargs={"tokenizer": self.tokenizer},
            num_proc=10,
            remove_columns=column_names,
            desc="Applying chat template to test_sft",
        )

    def local_dataset(self):  # TODO Only for local test, will remove later
        from utils.process_data import build_dataset

        train_dataset_split, processed_test_dataset = build_dataset(
            self.tokenizer, self.config_detail.dataset_name, self.config_detail.num_clients
        )
        self.train_dataset = train_dataset_split[0]
        self.test_dataset = processed_test_dataset

    def init_local_model(self):
        self.model, self.tokenizer = get_model_and_tokenizer(self.cfg_path)
        self.model.print_trainable_parameters()
        self.model.config.use_cache = (
            False  # silence the warnings. Please re-enable for inference!
        )
        if self.config_detail.sft.training_arguments.gradient_checkpointing:
            self.model.enable_input_require_grads()

    def initiate_local_training(self):
        self.model.config.use_cache = False
        self.params_dict_old = copy.deepcopy(
            OrderedDict(
                (name, param.detach())
                for name, param in self.model.named_parameters()
                if "default" in name
            )
        )
        self.params_dict_new = OrderedDict(
            (name, param.detach())
            for name, param in self.model.named_parameters()
            if "default" in name
        )
        self.model.state_dict = (
            lambda instance, *_, **__: get_peft_model_state_dict(
                instance, self.params_dict_new, "default"
            )
        ).__get__(self.model, type(self.model))

    def local_trainer_set(self):
        self.trainer = SFTTrainer(
            model=self.model,
            tokenizer=self.tokenizer,
            args=self.training_args,
            max_seq_length=self.config_detail.sft.max_seq_length,
            train_dataset=self.train_dataset,
            eval_dataset=self.test_dataset,
            dataset_text_field="text",
        )

    def train(self):
        results = self.trainer.train()
        return results.training_loss

    def save(self):
        new_adapter_weight = self.model.state_dict()
        single_output_dir = os.path.join(
            self.training_args.output_dir,
            "local_output_{}".format(self.client_id),
        )
        os.makedirs(single_output_dir, exist_ok=True)
        torch.save(new_adapter_weight, single_output_dir + "/pytorch_model.bin")

        older_adapter_weight = get_peft_model_state_dict(
            self.model, self.params_dict_old, "default"
        )
        set_peft_model_state_dict(self.model, older_adapter_weight, "default")

        return len(self.train_dataset), new_adapter_weight

    def start(self):
        self.init_local_model()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((self.host, self.port))
            print(f"Connected to {self.host}:{self.port}")

            # self.prepare_dataset()
            self.local_dataset()
            self.initiate_local_training()
            self.local_trainer_set()
            self.train()
            # No need to save the current training weights on the client.
            ## train_dataset_len, new_model_weight = self.save()
            # Only returns the weight file and related configuration information, without affecting the current client model weight.
            train_dataset_len, new_model_weight = (
                len(self.train_dataset),
                self.model.state_dict(),
            )
            if self.ldp is True:
                # Clipping
                new_model_weight, _ = clip_l2_norm(new_model_weight,
                                                   self.params_dict_old,
                                                   self.config_detail.sft.clip_threshold,
                                                   self.config_detail.model.device_map)
                # Add gaussian noise
                if self.config_detail.sft.dp_fedavg_gaussian_enabled is True:
                    std_dev = self.config_detail.sft.sensitivity * np.sqrt(
                        2 * np.log(1.25 / self.config_detail.sft.delta)
                    ) / self.config_detail.sft.epsilon
                    new_model_weight = local_add_gaussian_noise(new_model_weight,
                                                                std_dev,
                                                                self.config_detail.model.device_map)

            # Send updated weights
            data = pickle.dumps(
                {
                    "client_id": self.client_id,
                    "train_dataset_length": train_dataset_len,
                    "new_model_weight": new_model_weight,
                }
            )
            s.sendall(data)

        print("Training complete, weights sent to server")
        if self.use_chain is True:
            # record weight to chain
            send_weight(new_model_weight)

    def run_grpc_client(self):
        from .grpc_clients.grpc_client import grpc_connection
        from .grpc_clients.message import SEND_PARAMETERS, ClientSideMessage, ClientSideMetadata
        server_address = f"{self.config_detail.server.host}:50051"
        insecure = self.config_detail.client.grpc_insecure
        auth_cer = self.config_detail.client.grpc_auth_cer_path if self.config_detail.client.grpc_auth_cer_path is not None else None
        self.init_local_model()
        self.local_dataset()
        self.initiate_local_training()
        self.local_trainer_set()
        self.train()

        train_dataset_len, new_model_weight = (
            len(self.train_dataset),
            self.model.state_dict(),
        )
        if self.ldp is True:
            # Clipping
            new_model_weight, _ = clip_l2_norm(new_model_weight,
                                               self.params_dict_old,
                                               self.config_detail.sft.clip_threshold,
                                               self.config_detail.model.device_map)
            # Add gaussian noise
            if self.config_detail.sft.dp_fedavg_gaussian_enabled is True:
                std_dev = self.config_detail.sft.sensitivity * np.sqrt(
                    2 * np.log(1.25 / self.config_detail.sft.delta)
                ) / self.config_detail.sft.epsilon
                new_model_weight = local_add_gaussian_noise(new_model_weight,
                                                            std_dev,
                                                            self.config_detail.model.device_map)
        msg_content = {
            'client_id': self.client_id,
            'train_dataset_length': train_dataset_len,
            'new_model_weight': new_model_weight,
        }
        msg_data = ClientSideMessage(msg_content, ClientSideMetadata(SEND_PARAMETERS))

        with grpc_connection(server_address, insecure, auth_cer) as (receive, send):
            response = send(msg_data)
            print(f"Server response: {response.code}, {response.message}")

        if self.use_chain is True:
            # record weight to chain
            send_weight(new_model_weight)


if __name__ == "__main__":
    client = BaseClient(client_id="1233", cfg_path="../config.yaml")
    client.run_grpc_client()
