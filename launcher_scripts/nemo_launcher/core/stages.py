# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import functools
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import omegaconf
from nemo_launcher.core.launchers import AutoLauncher
from nemo_launcher.utils.data_utils.prepare_squad import (
    prepare_squad_for_fine_tuning,
    prepare_squad_for_prompt_learning,
)
from nemo_launcher.utils.job_utils import JobPaths
from omegaconf import OmegaConf


class NemoMegatronStage:
    """
    Base class for NeMo Megatron stages. All stages should build on top of this class.
    Call `run` function to run current stage.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.cluster = cfg.get("cluster_type")

        self.stage_name = None
        self.stage_cfg = None
        self.setup_stage_vars(cfg)
        self.job_name = self.stage_cfg.run.get("name")

    def setup_stage_vars(self, cfg: OmegaConf):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        raise NotImplementedError

    def run(self) -> str:
        """
        Run current stage returns job id on slurm based system otherwise empty string

        :return: job id on slurm based system otherwise empty string
        :rtype: str
        """
        # Setup folders and datasets
        self.setup_folder_and_data()
        # Save stage hydra config
        job_path = self.get_job_path()
        stage_cfg_path = NemoMegatronStage.save_stage_hydra_config(self.stage_cfg, job_path)
        # Make cluster parameters
        cluster_parameters = self._make_cluster_parameters(self.cluster)
        # Make command groups
        command_groups = self.make_stage_command_groups(stage_cfg_path)
        # Create launcher
        launcher = AutoLauncher(folder=job_path.folder, cluster=self.cluster, **cluster_parameters,)
        job_id = launcher.launch(command_groups=command_groups)

        return job_id

    def setup_folder_and_data(self) -> None:
        """Setup job/data folders and fine-tuning/prompt-learning dataset"""
        job_path = self.get_job_path()
        job_path.folder.mkdir(parents=True, exist_ok=True)
        results_folder = job_path.results_folder
        results_folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def save_stage_hydra_config(stage_cfg: OmegaConf, job_path: JobPaths) -> Path:
        """
        Interpolate and save hydra config file for current stage

        :param OmegaConf stage_cfg: current stage's hydra configuration
        :param JobPaths job_path: JobPaths object
        :return: path current stage's essential nemo scripts code
        :rtype: Path
        """
        _hydra_interpolation(stage_cfg)

        cfg_save_path = job_path.config_file
        omegaconf.OmegaConf.save(stage_cfg, cfg_save_path)
        return cfg_save_path

    def make_stage_command_groups(self, stage_cfg_path: Path) -> List[List[str]]:
        """
        Make the command groups for current stage
        Command groups is a list of command group. A command group is defined as:
              0. Command group is a list of command strings
              1. Each command group occupies one bcprun, srun or bash
              2. Each command group eventually has multiple commands connected by ";"

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command groups for current stage
        :rtype: List[List[str]]
        """
        raise NotImplementedError

    def _make_wandb_login_command(self) -> List[str]:
        """Make a command of login with w&b api key"""
        cfg = self.cfg
        wandb_cmd = ""
        if cfg.wandb_api_key_file is not None:
            with open(cfg.wandb_api_key_file, "r") as f:
                wandb_api_key = f.readline().rstrip()
            wandb_cmd = f"wandb login {wandb_api_key}"
        return [wandb_cmd]

    def _make_nemo_path_command(self) -> List[str]:
        """Extend nemo path to python path"""
        return [
            f"cd {self._nemo_code_path}",
            "git rev-parse HEAD",
            f'export PYTHONPATH={self._nemo_code_path}:\${{PYTHONPATH}}',
        ]

    # def _make_numa_mapping_command(self) -> List[str]:
    #     """Make a command of numa mapping call"""
    #     cfg = self.cfg
    #     numa_cfg = cfg.get("numa_mapping")
    #     if not numa_cfg.get("enable"):
    #         return []

    #     numa_override = [f"{k}={v}" for k, v in numa_cfg.items()]
    #     numa_command = [
    #         f"python3 -u {self._launcher_scripts_path / 'nemo_launcher/collections/numa_mapping.py'}",
    #         *numa_override,
    #     ]
    #     numa_command = " \\\n  ".join(numa_command)
    #     return [numa_command]

    def _make_api_log_command_prefix(self, results_dir: str) -> str:
        """Make a command prefix of api logging"""
        choice_model_type, choice_name = self.get_stage_config_choice()
        api_log = self.cfg.get("api_log", False)
        api_log_prefix = ""
        if api_log:
            api_log_path = os.path.join(results_dir, "api_logs")
            api_log_prefix = (
                "[[ \${SLURM_LOCALID} -eq 0 ]] && "
                f"API_LOG_CMD='apiLog.sh -p {choice_model_type}/{choice_name} -v nemo_launcher' || API_LOG_CMD=''; "
                f"LOGPATH={api_log_path} \${{API_LOG_CMD}}"
            )
        return api_log_prefix

    def _make_nsys_command_prefix(self, results_dir: str) -> str:
        """Make a command prefix of nsys profiling"""
        model_cfg = self.stage_cfg.get("model")
        if not model_cfg:
            return ""

        nsys_cfg = model_cfg.get("nsys_profile", None)
        nsys_prefix = ""
        if nsys_cfg is not None and nsys_cfg.get("enabled", False):
            profile_out_path = os.path.join(results_dir, "profile_logs")
            os.makedirs(profile_out_path, exist_ok=True)
            slurm_node = "\${SLURM_NODEID}"
            slurm_rank = "\${SLURM_PROCID}"
            slurm_jobid = "\${SLURM_JOB_ID}"
            nsys_prefix = (
                f"nsys profile -s none "
                f"-t {','.join(nsys_cfg.trace)} "
                f"-o {profile_out_path}/profile_{slurm_jobid}_node{slurm_node}_rank{slurm_rank} "
                f"--force-overwrite true "
                f"--capture-range=cudaProfilerApi "
                f"--capture-range-end=stop"
            )
        return nsys_prefix

    def _make_container_mounts_string(self) -> str:
        """
        Make container mounting string based on hydra configurations

        :return: container mounting string, e.g. "/path/to/A:/path/to/A,/path/to/B:/path/to/B,..."
        :rtype: str
        """

        def add_container_mounts(container_mounts):
            mounts_str = ""
            if container_mounts is not None:
                assert isinstance(
                    container_mounts, omegaconf.listconfig.ListConfig
                ), "container_mounts must be a list."
                for mount in container_mounts:
                    if mount is not None and isinstance(mount, str):
                        mounts_str += f",{mount}" if ":" in mount else f",{mount}:{mount}"
            return mounts_str

        cfg = self.cfg
        data_dir = cfg.get("data_dir")
        base_results_dir = cfg.get("base_results_dir")
        mounts_string = f"{self._launcher_scripts_path}:{self._launcher_scripts_path},{data_dir}:{data_dir},{base_results_dir}:{base_results_dir}"

        container_mounts = cfg.get("container_mounts")
        mounts_string += add_container_mounts(container_mounts)
        return mounts_string

    def _make_cluster_parameters(self, cluster: str) -> Dict:
        """
        Make a cluster-specific parameters for jobs on different clusters.
        Current clusters include bcm(slurm), bcp and interactive.
        For example for bcm, it will return slurm parameters:
            {'job_name': 'some_name', 'nodes': 2, 'ntasks_per_node': 8, ...}

        :param str cluster: i.e. `bcm`, `bcp`, `interactive`, etc.
        :return: a dictionary of cluster parameters, e.g. `ntasks_per_node`
        :rtype: Dict
        """
        cfg = self.cfg
        stage_cfg = self.stage_cfg

        run_cfg = stage_cfg.get("run")
        job_name = run_cfg.get("name")
        time_limit = run_cfg.get("time_limit")
        nodes = run_cfg.get("nodes")
        dependency = run_cfg.get("dependency")
        if nodes is None:
            nodes = stage_cfg.get("trainer").get("num_nodes")
        ntasks_per_node = run_cfg.get("ntasks_per_node")
        if ntasks_per_node is None:
            ntasks_per_node = stage_cfg.get("trainer").get("devices")

        container_image = cfg.get("container")
        container_mounts = self._make_container_mounts_string()

        setup = None
        env_vars = self.get_env_vars()
        if env_vars:
            setup = [f"export {k}={v}" for k, v in env_vars.items()]

        cluster_parameters = {}
        shared_parameters = {
            "job_name": job_name,
            "nodes": nodes,
            "time": time_limit,
            "ntasks_per_node": ntasks_per_node,
            "setup": setup,
        }
        if cluster == "bcm":
            cluster_cfg = cfg.get("cluster")
            if cfg.get("training").get("model").get("ub_tp_comm_overlap", False):
                if "srun_args" not in cluster_cfg:
                    cluster_cfg["srun_args"] = []
                cluster_cfg["srun_args"] += ["--mpi=pmix"]
            slurm_cfg = {**copy.deepcopy(cluster_cfg)}
            job_name_prefix = slurm_cfg.pop("job_name_prefix")
            cluster_parameters = {**slurm_cfg}
            cluster_parameters.update(
                {
                    **shared_parameters,
                    "dependency": dependency,
                    "container_image": container_image,
                    "container_mounts": container_mounts,
                }
            )
            cluster_parameters["job_name"] = job_name_prefix + cluster_parameters["job_name"]
        elif cluster == "bcp":
            cluster_parameters.update(
                {**shared_parameters, "env_vars": env_vars,}
            )
        elif cluster == "interactive":
            cluster_parameters.update(shared_parameters)

        return cluster_parameters

    def get_env_vars(self) -> Dict:
        """
        Set up dictionary for environment variables
        The environment variables from hydra config will be set inside the job scripts.
        For Example:
            Set `env_vars.NVTE_BIAS_DROPOUT_FUSION=1` while calling nemo_launcherlauncher-scripts,
            `NVTE_BIAS_DROPOUT_FUSION=1` will be set while running the job.

        :return: a dictionary of env vars while running the job.
        :rtype: Dict
        """
        env_vars = {k: v for k, v in self.cfg.get("env_vars").items() if v is not None}
        return env_vars

    def get_stage_config_choice(self):
        """
        Return current stages config's corresponding `choice_model_type` and `choice_name`
        For example, if `training=gpt3/5b`, then `choice_model_type=gpt3` and `choice_name=5b`
        """
        stage_config_choice = self.cfg.get(f"{self.stage_name}_config")
        choice_model_type = stage_config_choice.rsplit("/", 1)[0]
        choice_name = stage_config_choice.rsplit("/", 1)[1]
        return choice_model_type, choice_name

    @property
    def _launcher_scripts_path(self) -> Path:
        return Path(self.cfg.get("launcher_scripts_path"))

    @property
    def _nemo_code_path(self) -> Path:
        return Path("/opt/NeMo")

    @property
    def _data_dir(self) -> Path:
        return Path(self.cfg.get("data_dir"))

    @property
    def _cuda_visible_devices(self) -> str:
        ntasks_per_node = self.stage_cfg.run.get("ntasks_per_node")
        if ntasks_per_node is None:
            ntasks_per_node = self.stage_cfg.trainer.get("devices", 1)
        return (
            "CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7"
            if ntasks_per_node == 8
            else f"CUDA_VISIBLE_DEVICES={','.join(map(str, range(ntasks_per_node)))}"
        )

    @property
    def _cuda_device_max_connections(self) -> str:
        model_cfg = self.stage_cfg.get("model")
        if not model_cfg:
            return ""
        tensor_model_parallel_size = model_cfg.get("tensor_model_parallel_size", 1)
        return "CUDA_DEVICE_MAX_CONNECTIONS=1" if tensor_model_parallel_size > 1 else ""

    @property
    def _nvte_bias_gelu_nvfusion(self) -> str:
        """Only used in pretraining; override in training class"""
        return ""

    @functools.lru_cache()
    def get_job_path(self, sub_stage: Optional = None) -> JobPaths:
        """Fetch a JobPaths object for current stage"""
        run_cfg = self.stage_cfg.get("run")
        results_dir = Path(run_cfg.get("results_dir"))  # TODO: rename this to job dir in config
        if sub_stage is not None:
            results_dir = results_dir / sub_stage
        return JobPaths(results_dir, self.job_name)

    @property
    def _set_ln_sm_margin(self) -> str:
        """ Set LayerNorm SM margin when using P2P communication overlap to support the overlap with LayerNorm kernel """
        if (self.cfg.training.model.get("overlap_p2p_comm", False) and
            self.cfg.training.model.get("pipeline_model_parallel_size") > 1 and
            self.cfg.training.model.get("virtual_pipeline_model_parallel_size") > 1):
            get_ln_sm_margin_command = (
                f"python3 {self._launcher_scripts_path / 'nemo_launcher/collections/conditional_cfgs.py'} "
                f"name=get_ln_sm_margin"
            )
            return f"NVTE_FWD_LAYERNORM_SM_MARGIN=\$({get_ln_sm_margin_command}) NVTE_BWD_LAYERNORM_SM_MARGIN=\$({get_ln_sm_margin_command})"
        return ""

    @property
    def _skip_ag_overlap(self) -> str:
        """ Skip TP-AllGather overlap with ring-exchange at (1) bf16 and (2) PP > 1 """
        if (self.cfg.training.model.get("ub_tp_comm_overlap", False) and
            self.cfg.training.model.get("pipeline_model_parallel_size") > 1):
            use_fp8 = self.cfg.training.model.get("fp8", False)
            get_ag_overlap_command = (
                f"python3 {self._launcher_scripts_path / 'nemo_launcher/collections/conditional_cfgs.py'} "
                f"name=get_ag_overlap "
                f"fp8={use_fp8} "
            )
            return f"NVTE_UB_SPLIT_AG=\$({get_ag_overlap_command})"
        return ""


class NeMoStage(NemoMegatronStage):
    """
    Stage is a nemo stage if it uses a nemo scripts
    Current nemo stage includes:
        - pretraining
        - fine-tuning
        - prompt-learning
        - t5/mt5 eval
    GPT3 eval is not a NeMo stage because it uses eval-harness inside nemo_launcher collections.
    """

    def make_stage_command_groups(self, stage_cfg_path: Path) -> List[List[str]]:
        """
        Make the command groups for current stage
        Command groups is a list of command group. A command group is defined as:
              0. Command group is a list of command strings
              1. Each command group occupies one bcprun, srun or bash
              2. Each command group eventually has multiple commands connected by ";"

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command groups for current stage
        :rtype: List[List[str]]
        """
        # Training has one command group
        # Shared with fine-tuning and prompt learning
        command_groups = [[]]
        command_groups[0] += self._make_wandb_login_command()
        command_groups[0] += self._make_nemo_path_command()
        # command_groups[0] += self._make_numa_mapping_command()

        # _cuda_device_max_connections and _cuda_visible_devices cannot be used as command prefix on BCP
        if self.cluster == "bcp":
            core_command = []
        else:
            core_command = [
                self._cuda_device_max_connections,
                self._cuda_visible_devices,
                self._set_ln_sm_margin,
                self._skip_ag_overlap,
                self._nvte_bias_gelu_nvfusion,
            ]

        core_command += [
            self._make_api_log_command_prefix(results_dir=self.get_job_path().results_folder),
            self._make_nsys_command_prefix(results_dir=self.get_job_path().results_folder),
            self._make_nemo_call_string(stage_cfg_path),
        ]
        core_command_string = " ".join([c for c in core_command if c])
        command_groups[0] += [core_command_string]
        command_groups = clean_command_groups(command_groups)

        return command_groups

    def _make_nemo_call_string(self, stage_cfg_path: Path) -> str:
        """
        Make nemo scripts calling command string
        This is for current nemo stage's essential nemo script calling.

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command string of nemo script calling
        :rtype: str
        """
        choice_model_type, choice_name = self.get_stage_config_choice()
        code_path = self._get_nemo_code_path(choice_model_type)

        hydra_override = self._make_hydra_override()

        command = [
            f"python3 -u {code_path} ",
            f"--config-path={stage_cfg_path.parents[0]}",
            f"--config-name={stage_cfg_path.name}",
            *hydra_override,
        ]
        command_string = " \\\n  ".join(command)
        return command_string

    def _make_hydra_override(self) -> List:
        """
        Override some existing hydra configurations if necessary.
        
        Example use cases are:
            1. For bcp cluster, `+rank=\${RANK}` is required running some NeMo scripts.
                Existing hydra config doesn't have `rank` field, so we overwrite on the fly.
            2. Auto blend training dataset by overwriting empty `model.data.data_prefix` as 
                `model.data.data_prefix=\$({auto_blend_command})`. Existing `model.data.data_prefix`
                could be None in cfg, so we overwrite it in this function.
        """
        hydra_override = []
        if self.cluster == "bcp":
            hydra_override += ["+rank=\${RANK}"]
        return hydra_override

    def get_env_vars(self) -> Dict:
        """
        Set up dictionary for environment variables
        The environment variables from hydra config will be set inside the job scripts.
        For Example:
            Set `env_vars.NVTE_BIAS_DROPOUT_FUSION=1` while calling nemo_launcherlauncher-scripts,
            `NVTE_BIAS_DROPOUT_FUSION=1` will be set while running the job.

        :return: a dictionary of env vars while running the job.
        :rtype: Dict
        """
        env_vars = super().get_env_vars()
        devices = self.stage_cfg.trainer.get("devices", 1)
        if self.cluster != "bcm":
            env_vars["SLURM_NTASKS_PER_NODE"] = devices
        if self.cluster == "bcp":  # Set env prefix as env var on BCP
            for env_var_str in [self._cuda_device_max_connections, self._cuda_visible_devices, self._set_ln_sm_margin, self._skip_ag_overlap,]:
                if env_var_str:
                    var_name, var_val = env_var_str.split("=")
                    env_vars[var_name] = var_val
        return env_vars


class Training(NeMoStage):
    """Stage class of pretraining with NeMo scripts"""

    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "training"
        self.stage_cfg = cfg.get("training")

    def _make_hydra_override(self) -> List:
        """
        Override some existing hydra configurations if necessary.
        Example use cases are:
            1. For bcp cluster, `+rank=\${RANK}` is required running some NeMo scripts.
                Existing hydra config doesn't have `rank` field, so we overwrite on the fly.
            2. Auto blend training dataset by overwriting empty `model.data.data_prefix` as 
                `model.data.data_prefix=\$({auto_blend_command})`. Existing `model.data.data_prefix`
                could be None in cfg, so we overwrite it in this function.

        :return: hydra override string added in nemo script calling
        :rtype: str
        """
        hydra_override = []
        choice_model_type, choice_name = self.get_stage_config_choice()
        if self.cluster == "bcp":
            hydra_override += ["+rank=\${RANK}"]
        if self.stage_cfg.model.data.get("data_prefix", None) is None:
            preprocessed_dir = self.stage_cfg.run.get("preprocessed_dir")
            blending_alpha = self.stage_cfg.run.get("blending_alpha")
            auto_blend_command = (
                f"python3 {self._launcher_scripts_path / 'nemo_launcher/collections/auto_blend.py'} "
                f"model_type={choice_model_type} "
                f"preprocessed_dir={preprocessed_dir} "
                f"blending_alpha={blending_alpha}"
            )
            hydra_override += [f"model.data.data_prefix=\$({auto_blend_command})"]
        if self.stage_cfg.model.get("ub_tp_comm_overlap", False):
            get_ub_cfg_file_command = self._get_ub_cfg_file()
            hydra_override += [f"+model.ub_tp_comm_overlap_cfg=\$({get_ub_cfg_file_command})"]
        if self.stage_cfg.model.get("gc_interval", 0) > 1:
            gc_interval = min(self.stage_cfg.model.get("gc_interval"), self.cfg.training.trainer.get("val_check_interval"))
            hydra_override += [f"model.gc_interval={gc_interval}"]
        return hydra_override

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.

        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        model_type_to_code_path = {
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_pretraining.py",
            "mt5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_pretraining.py",
            "gpt3": self._nemo_code_path / "examples/nlp/language_modeling/megatron_gpt_pretraining.py",
            "bert": self._nemo_code_path / "examples/nlp/language_modeling/megatron_bert_pretraining.py",
        }
        return model_type_to_code_path[model_type]

    def _get_ub_cfg_file(self) -> str:
        """
        Spawn the script to search UB configuration file
        """
        tp_size = self.stage_cfg.model.get("tensor_model_parallel_size")
        hidden_size = self.stage_cfg.model.get("hidden_size")
        mb_size = self.stage_cfg.model.get("micro_batch_size")
        seqlen = self.stage_cfg.model.get("encoder_seq_length")
        ub_cfg_path = os.path.join(self._launcher_scripts_path, "launcher_scripts/conf/training/gpt3/ub-confs")

        get_ub_cfg_file_command = (
            f"python3 {self._launcher_scripts_path / 'nemo_launcher/collections/conditional_cfgs.py'} "
            f"name=get_ub_cfg_file "
            f"ub_cfg_path={ub_cfg_path} "
            f"tp_size={tp_size} "
            f"hidden_size={hidden_size} "
            f"mb_size={mb_size} "
            f"seqlen={seqlen}"
        )
        return get_ub_cfg_file_command


class FineTuning(NeMoStage):
    """Stage class of fine-tuning with NeMo scripts"""

    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "fine_tuning"
        self.stage_cfg = cfg.get("fine_tuning")

    def setup_folder_and_data(self) -> None:
        """Setup job/data folders and fine-tuning/prompt-learning dataset"""
        super().setup_folder_and_data()

        # Prepare fine-tuning dataset
        data_dir = self.cfg.get("data_dir")
        task_name = self.stage_cfg.run.get("task_name")

        # GLUE for internal use
        download_glue_script_path = self._launcher_scripts_path / "nemo_launcher/utils/data_utils/download_glue.py"
        if download_glue_script_path.exists():
            from nemo_launcher.utils.data_utils.download_glue import TASKS_LOWER, download_glue

            if task_name in TASKS_LOWER:
                download_glue(data_dir=os.path.join(data_dir, "glue_data"), tasks=task_name)

        # Prepare dataset for squad
        if task_name in ["squad", "xquad"]:
            prepare_squad_for_fine_tuning(data_dir=os.path.join(data_dir, "squad_data"))

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.
        
        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        if model_type == "gpt3":
            raise NotImplementedError("Fine-tuning is not supported in NeMo Megatron GPT-3 models.")
        model_type_to_code_path = {
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_seq2seq_finetune.py",
            "mt5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_seq2seq_finetune.py",
        }
        return model_type_to_code_path[model_type]


class PromptLearning(NeMoStage):
    """Stage class of prompt-learning with NeMo scripts"""

    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "prompt_learning"
        self.stage_cfg = cfg.get("prompt_learning")

    def setup_folder_and_data(self) -> None:
        """Setup job/data folders and fine-tuning/prompt-learning dataset"""
        # Setup folders
        super().setup_folder_and_data()

        # Prepare prompt learning dataset
        data_dir = self.cfg.get("data_dir")
        task_name = self.stage_cfg.run.get("task_name")
        # Prepare squad dataset
        if task_name == 'squad':
            prepare_squad_for_prompt_learning(
                os.path.join(data_dir, "prompt_data"), self._launcher_scripts_path,
            )

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.
        
        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        model_type_to_code_path = {
            "gpt3": self._nemo_code_path / "examples/nlp/language_modeling/megatron_gpt_prompt_learning.py",
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_prompt_learning.py",
            "mt5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_prompt_learning.py",
        }
        return model_type_to_code_path[model_type]


class AdapterLearning(PromptLearning):
    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "adapter_learning"
        self.stage_cfg = cfg.get("adapter_learning")

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.
        
        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        model_type_to_code_path = {
            "gpt3": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_gpt_adapter_tuning.py",
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_t5_adapter_tuning.py",
        }
        return model_type_to_code_path[model_type]


class IA3Learning(PromptLearning):
    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "ia3_learning"
        self.stage_cfg = cfg.get("ia3_learning")

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.
        
        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        model_type_to_code_path = {
            "gpt3": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_gpt_ia3_tuning.py",
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_t5_ia3_tuning.py",
        }
        return model_type_to_code_path[model_type]


class Conversion(NemoMegatronStage):
    """Stage class of converting training checkpoints to .nemo format"""

    def setup_stage_vars(self, cfg: OmegaConf):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "conversion"
        self.stage_cfg = cfg.get("conversion")

    def _make_hparams_override_command(self):
        """
        Make the command string to override some fields in hparams.yaml file while converting checkpoint into .nemo format

        :return: command string for hparams override with the script in collections
        :rtype: str
        """
        model_cfg = self.stage_cfg.get("model")
        hparams_file = model_cfg.get("hparams_file")
        vocab_file = model_cfg.get("vocab_file")
        merge_file = model_cfg.get("merge_file")
        tokenizer_model = model_cfg.get("tokenizer_model")
        override_configs = {
            "hparams_file": hparams_file,
            "output_path": self.get_job_path().results_folder,
            "vocab_file": vocab_file,
            "merge_file": merge_file,
            "tokenizer_model": tokenizer_model,
        }
        hparams_override = [f"{k}={v}" for k, v in override_configs.items()]
        override_command = [
            f"python3 -u {self._launcher_scripts_path / 'nemo_launcher/collections/hparams_override.py'}",
            *hparams_override,
        ]
        override_command = " \\\n  ".join(override_command)
        return [override_command]

    def _make_checkpoint_search_command(self, **kwargs: Any) -> str:
        """
        Make the command string to search for the latest checkpoint inside checkpoint folder

        :param Path **kwargs: checkpoint search script's argument override
        :return: command string for searching for latest checkpoint with the script in collections
        :rtype: str
        """
        checkpoint_override = [f"{k}={v}" for k, v in kwargs.items()]
        return (
            f"python3 {self._launcher_scripts_path / 'nemo_launcher/collections/checkpoint_search.py'} "
            f"{' '.join(checkpoint_override)}"
        )

    def make_stage_command_groups(self, stage_cfg_path: Path) -> List[List[str]]:
        """
        Make the command groups for current stage
        Command groups is a list of command group. A command group is defined as:
              0. Command group is a list of command strings
              1. Each command group occupies one bcprun, srun or bash
              2. Each command group eventually has multiple commands connected by ";"

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command groups for current stage
        :rtype: List[List[str]]
        """
        command_groups = [[], []]
        command_groups[0] += self._make_hparams_override_command()

        run_cfg = self.stage_cfg.get("run")
        model_cfg = self.stage_cfg.get("model")
        checkpoint_search_command = self._make_checkpoint_search_command(
            checkpoint_folder=model_cfg.get("checkpoint_folder"),
            checkpoint_name=model_cfg.get("checkpoint_name"),
            tensor_model_parallel_size=model_cfg.get("tensor_model_parallel_size"),
            pipeline_model_parallel_size=model_cfg.get("pipeline_model_parallel_size"),
        )
        command_groups[-1] += [f"export CKPT_NAME=$({checkpoint_search_command})"]

        nemo_file_name = run_cfg.get("nemo_file_name")
        hparams_override_file = self.get_job_path().results_folder / "hparams_override.yaml"
        nemo_file_path = self.get_job_path().results_folder / nemo_file_name
        code_path = self._nemo_code_path / "examples/nlp/language_modeling/megatron_ckpt_to_nemo.py"
        args = create_args_list(
            replace_underscore=False,
            gpus_per_node=run_cfg.get("ntasks_per_node"),
            model_type=model_cfg.get("model_type"),
            checkpoint_folder=model_cfg.get("checkpoint_folder"),
            checkpoint_name="\${CKPT_NAME}",
            hparams_file=hparams_override_file,
            nemo_file_path=nemo_file_path,
            tensor_model_parallel_size=model_cfg.get("tensor_model_parallel_size"),
            pipeline_model_parallel_size=model_cfg.get("pipeline_model_parallel_size"),
        )
        if model_cfg.get("pipeline_model_parallel_split_rank") is not None:
            args += create_args_list(
                replace_underscore=False,
                pipeline_model_parallel_split_rank=model_cfg.get("pipeline_model_parallel_split_rank"),
            )

        args += ["--bcp"] if self.cluster == "bcp" else []

        core_command = [f"python3 -u {code_path}", *args]
        core_command_string = " \\\n  ".join(core_command)
        command_groups[-1] += [core_command_string]
        command_groups = clean_command_groups(command_groups)

        return command_groups


class NeMoEvaluation(NeMoStage):
    """
        Stage class of gpt3/t5/mt5 evaluation with NeMo scripts
        Including: fine-tuning eval, prompt-learning eval, adapter/ia3 learning eval
    """

    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "evaluation"
        self.stage_cfg = cfg.get("evaluation")

    def make_stage_command_groups(self, stage_cfg_path: Path) -> List[List[str]]:
        """
        Make the command groups for current stage
        Command groups is a list of command group. A command group is defined as:
              0. Command group is a list of command strings
              1. Each command group occupies one bcprun, srun or bash
              2. Each command group eventually has multiple commands connected by ";"

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command groups for current stage
        :rtype: List[List[str]]
        """
        command_groups = super().make_stage_command_groups(stage_cfg_path)

        choice_model_type, choice_name = self.get_stage_config_choice()
        if any([choice_model_type.startswith(type) for type in ["prompt", "ia3", "adapter"]]):
            pred_file_path = self.stage_cfg.get("pred_file_path")
            ground_truth_file_path = self.stage_cfg.get("ground_truth_file_path")
            code_path = (
                self._launcher_scripts_path / "nemo_launcher/collections/metric_calculation/squad_metric_calc.py"
            )
            args = create_args_list(pred=pred_file_path, ground_truth=ground_truth_file_path,)
            split_string = self.stage_cfg.get("split_string", None)
            if split_string:
                args += create_args_list(split_string=f"'{split_string}'")
            calculation_command = [f"python3 {code_path}", *args]
            calculation_command = " \\\n  ".join(calculation_command)
        elif choice_name == "squad":
            output_file_path_prefix = self.stage_cfg.model.data.validation_ds.get("output_file_path_prefix")
            pred_file_path = output_file_path_prefix + "_validation_dataloader0_inputs_preds_labels.json"
            ground_truth_file_path = self.stage_cfg.model.data.validation_ds.get("ground_truth_file_path")
            code_path = (
                self._launcher_scripts_path / "nemo_launcher/collections/metric_calculation/fine_tuning_metric_calc.py"
            )
            args = create_args_list(
                replace_underscore=False,
                pred_file=pred_file_path,
                target_file=ground_truth_file_path,
                squad_eval_script_path=self._launcher_scripts_path
                / "nemo_launcher/collections/metric_calculation/squad_metric_calc.py",
            )
            calculation_command = [f"python3 {code_path}", *args]
            calculation_command = " \\\n  ".join(calculation_command)
        else:
            calculation_command = None

        if calculation_command is not None:
            command_groups += [[calculation_command]]
        return command_groups

    def _get_nemo_code_path(self, model_type: str) -> Path:
        """
        Provide the essential nemo code path for running the stage, usually different model types use different nemo scripts.
        For example, `megatron_t5_pretraining.py` for t5 and `megatron_gpt_pretraining.py` for gpt3.
        
        :param str model_type: i.e. `gpt3`, `t5`, `mt5`, etc.
        :return: path current stage's essential nemo scripts code 
        :rtype: Path
        """
        if model_type in ["gpt3", "prompt_gpt3"]:
            raise ValueError("Evaluating GPT-3 models needs `EvalHarnessEvaluation` class.")
        model_type_to_code_path = {
            "t5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_seq2seq_eval.py",
            "mt5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_seq2seq_eval.py",
            "prompt_t5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_prompt_learning_eval.py",
            "prompt_mt5": self._nemo_code_path / "examples/nlp/language_modeling/megatron_t5_prompt_learning_eval.py",
            "ia3_t5": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_t5_ia3_eval.py",
            "ia3_gpt3": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_gpt_ia3_eval.py",
            "adapter_t5": self._nemo_code_path / "examples/nlp/language_modeling/tuning/megatron_t5_adapter_eval.py",
            "adapter_gpt3": self._nemo_code_path
            / "examples/nlp/language_modeling/tuning/megatron_gpt_adapter_eval.py",
        }
        return model_type_to_code_path[model_type]


class EvalHarnessEvaluation(NemoMegatronStage):
    """Stage class of gpt-3 evaluation harness"""

    def __init__(self, cfg):
        super().__init__(cfg)
        choice_model_type, choice_name = self.get_stage_config_choice()
        self.prompt_evaluation = choice_model_type == "prompt_gpt3"

    def setup_stage_vars(self, cfg):
        """Setup the stage vars, i.e. stage name and stage cfg"""
        self.stage_name = "evaluation"
        self.stage_cfg = cfg.get("evaluation")

    def _make_download_command_string(self) -> str:
        """
        Make dataset download command for evaluation harness.

        :return: command string of downloading evaluation data
        :rtype: str
        """
        data_dir = self.cfg.get("data_dir")
        cache_dir = os.path.join(data_dir, "eval_harness_data")
        run_cfg = self.stage_cfg.get("run")
        tasks = run_cfg.get("tasks")

        code_path = self._launcher_scripts_path / "nemo_launcher/collections/eval_harness/download.py"
        args = create_args_list(tasks=tasks, cache_dir=cache_dir,)
        download_command = [f"python3 {code_path}", *args]
        download_command_string = " \\\n  ".join(download_command)
        return download_command_string

    def make_stage_command_groups(self, stage_cfg_path: Path) -> List[List[str]]:
        """
        Make the command groups for current stage
        Command groups is a list of command group. A command group is defined as:
              0. Command group is a list of command strings
              1. Each command group occupies one bcprun, srun or bash
              2. Each command group eventually has multiple commands connected by ";"

        :param Path stage_cfg_path: path to interpolated and saved configuration
        :return: command groups for current stage
        :rtype: List[List[str]]
        """
        if self.prompt_evaluation:
            command_groups = [[]]
        else:
            command_groups = [[], []]
            command_groups[0] += [self._make_download_command_string()]

        data_dir = self.cfg.get("data_dir")
        cache_dir = os.path.join(data_dir, "eval_harness_data")
        run_cfg = self.stage_cfg.get("run")
        model_cfg = self.stage_cfg.get("model")

        code_path = self._launcher_scripts_path / "nemo_launcher/collections/eval_harness/evaluate.py"
        args = create_args_list(
            replace_underscore=False,
            name=run_cfg.get("name"),
            model=model_cfg.get("model_type"),
            tasks=run_cfg.get("tasks"),
            cache_dir=cache_dir,
            output_path=self.get_job_path().results_folder,
            batch_size=model_cfg.get("eval_batch_size"),
            tensor_model_parallel_size=model_cfg.get("tensor_model_parallel_size"),
            pipeline_model_parallel_size=model_cfg.get("pipeline_model_parallel_size"),
            precision=model_cfg.get("precision"),
        )

        if self.prompt_evaluation:
            args += create_args_list(
                replace_underscore=False,
                nemo_model=model_cfg.get("nemo_model"),
                prompt_dataset_paths=model_cfg.get("prompt_dataset_paths"),
            )
        else:
            # GPT evaluation
            args += create_args_list(
                replace_underscore=False,
                vocab_file=model_cfg.get("vocab_file"),
                merge_file=model_cfg.get("merge_file"),
                nemo_model=model_cfg.get("nemo_model"),
                checkpoint_folder=model_cfg.get("checkpoint_folder"),
                checkpoint_name=model_cfg.get("checkpoint_name"),
                hparams_file=model_cfg.get("hparams_file"),
            )

        core_command = [f"python3 -u {code_path}", *args]
        core_command_string = " \\\n  ".join(core_command)
        command_groups[-1] += [core_command_string]
        command_groups = clean_command_groups(command_groups)

        return command_groups


def clean_command_groups(command_groups: List[List[str]]) -> List[List[str]]:
    """
    Remove empty command group in command groups

    :param List[List[str]] command_groups: command groups is a list of command group
    :return: cleaned command groups
    :rtype: List[List[str]]
    """
    for ind, command_group in enumerate(command_groups):
        command_groups[ind] = [c for c in command_group if c]
    return command_groups


def _hydra_interpolation(cfg: OmegaConf) -> None:
    """
    Interpolate hydra config values in cfg object, bypassing lazy interpolation

    :param OmegaConf cfg: OmegaConf object with the config to be interpolated
    :return: None
    """

    def interpolate(cfg: OmegaConf):
        if isinstance(cfg, omegaconf.dictconfig.DictConfig):
            for k, v in cfg.items():
                cfg[k] = interpolate(v)
        elif isinstance(cfg, omegaconf.listconfig.ListConfig):
            for i, v in enumerate(cfg):
                cfg[i] = interpolate(v)
        return cfg

    interpolate(cfg)


def create_args_list(hydra: bool = False, replace_underscore: bool = True, **kwargs: Any,) -> List[str]:
    """
    An easy tool function to convert arguments into a list of argument strings.
    For example, `create_args_list(a=123, b=456)` will generate `['--a=123', '--b=456']`.

    :param bool hydra: Either a hydra argument or regular argument, `--` will be added to regular arguments
    :param bool replace_underscore: Whether to replace `_` with `-` in arguments' names.
    :params Any **kwargs: argument name and their value
    :return: A list of argument strings, e.g. `['--a=123', '--b=456', ...]`
    :rtype: List[str]
    """

    args = []
    for k, v in kwargs.items():
        if hydra:
            args.append(f"{k}={v}")
        else:
            # use "store_true" to add keys only args
            if replace_underscore:
                k = k.replace("_", "-")
            args.append(f"--{k}" if v == "store_true" else f"--{k}={v}")
    return args
