"""Setup dependencies & services for instances."""
from concurrent import futures
import hashlib
import os
import time
from typing import Dict, List, Optional, Tuple

from sky import sky_logging
from sky.provision import common
from sky.provision import metadata_utils
from sky.skylet import constants
from sky.utils import command_runner
from sky.utils import common_utils
from sky.utils import subprocess_utils
from sky.utils import ux_utils

logger = sky_logging.init_logger(__name__)

_MAX_RETRY = 5

# Increase the limit of the number of open files for the raylet process,
# as the `ulimit` may not take effect at this point, because it requires
_RAY_PRLIMIT = (
    'which prlimit && for id in $(pgrep -f raylet/raylet); '
    'do sudo prlimit --nofile=1048576:1048576 --pid=$id || true; done;')

_DUMP_RAY_PORTS = (
    'python -c \'import json, os; '
    f'json.dump({constants.SKY_REMOTE_RAY_PORT_DICT_STR}, '
    f'open(os.path.expanduser("{constants.SKY_REMOTE_RAY_PORT_FILE}"), "w"))\'')

# Command that calls `ray status` with SkyPilot's Ray port set.
RAY_STATUS_WITH_SKY_RAY_PORT_COMMAND = (
    'RAY_PORT=$(python -c "from sky.skylet import job_lib; '
    'print(job_lib.get_ray_port())" 2> /dev/null || echo 6379);'
    'RAY_ADDRESS=127.0.0.1:$RAY_PORT ray status')

# Restart skylet when the version does not match to keep the skylet up-to-date.
_MAYBE_SKYLET_RESTART_CMD = 'python3 -m sky.skylet.attempt_skylet'


def _auto_retry(func):

    def retry(*args, **kwargs):
        backoff = common_utils.Backoff(initial_backoff=1, max_backoff_factor=5)
        for retry_cnt in range(_MAX_RETRY):
            try:
                return func(*args, **kwargs)
            except Exception as e:  # pylint: disable=broad-except
                if retry_cnt >= _MAX_RETRY - 1:
                    raise e
                sleep = backoff.current_backoff()
                logger.info(f'Retrying in {sleep:.1f} seconds.')
                time.sleep(sleep)

    return retry


def _parallel_ssh_with_cache(func, cluster_name: str, stage_name: str,
                             digest: str,
                             cluster_metadata: common.ClusterMetadata,
                             ssh_credentials: Dict[str, str]) -> None:
    with futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = []
        for instance_id, metadata in cluster_metadata.instances.items():
            runner = command_runner.SSHCommandRunner(metadata.get_feasible_ip(),
                                                     port=22,
                                                     **ssh_credentials)
            wrapper = metadata_utils.cache_func(cluster_name, instance_id,
                                                stage_name, digest)
            log_dir_abs = metadata_utils.get_instance_log_dir(
                cluster_name, instance_id)
            log_path_abs = str(log_dir_abs / (stage_name + '.log'))
            logger.info(f'Running {stage_name} on {instance_id} - logging to '
                        f'{log_path_abs}')
            results.append(
                pool.submit(wrapper(func), runner, metadata, log_path_abs))

        for future in results:
            future.result()


def internal_dependencies_setup(cluster_name: str, setup_commands: List[str],
                                cluster_metadata: common.ClusterMetadata,
                                ssh_credentials: Dict[str, str]) -> None:
    """Setup internal dependencies."""
    # compute the digest
    digests = []
    for cmd in setup_commands:
        digests.append(hashlib.sha256(cmd.encode()).digest())
    hasher = hashlib.sha256()
    for d in digests:
        hasher.update(d)
    digest = hasher.hexdigest()

    @_auto_retry
    def _setup_node(runner: command_runner.SSHCommandRunner,
                    metadata: common.InstanceMetadata, log_path: str):
        del metadata
        for cmd in setup_commands:
            returncode, stdout, stderr = runner.run(cmd,
                                                    stream_logs=False,
                                                    log_path=log_path,
                                                    require_outputs=True)
            if returncode:
                raise RuntimeError(
                    'Failed to run setup commands on an instance. '
                    f'(exit code {returncode}). Error: '
                    f'===== stdout ===== \n{stdout}\n'
                    f'===== stderr ====={stderr}')

    _parallel_ssh_with_cache(_setup_node,
                             cluster_name,
                             stage_name='internal_dependencies_setup',
                             digest=digest,
                             cluster_metadata=cluster_metadata,
                             ssh_credentials=ssh_credentials)


@_auto_retry
def start_ray_head_node(cluster_name: str, custom_resource: Optional[str],
                        cluster_metadata: common.ClusterMetadata,
                        ssh_credentials: Dict[str, str]) -> None:
    """Start Ray on the head node."""
    ssh_runner = command_runner.SSHCommandRunner(
        cluster_metadata.get_feasible_ips()[0], port=22, **ssh_credentials)
    assert cluster_metadata.head_instance_id is not None, (cluster_name,
                                                           cluster_metadata)
    log_dir = metadata_utils.get_instance_log_dir(
        cluster_name, cluster_metadata.head_instance_id)
    log_path_abs = str(log_dir / ('ray_cluster' + '.log'))
    ray_options = (
        f'--port={constants.SKY_REMOTE_RAY_PORT} '
        f'--dashboard-port={constants.SKY_REMOTE_RAY_DASHBOARD_PORT} '
        f'--object-manager-port=8076 '
        f'--temp-dir={constants.SKY_REMOTE_RAY_TEMPDIR}')
    if custom_resource:
        ray_options += f' --resources=\'{custom_resource}\''

    # TODO(zhwu): add the output to log files.
    returncode, stdout, stderr = ssh_runner.run(
        'ray stop; unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; '
        'RAY_SCHEDULER_EVENTS=0 RAY_DEDUP_LOGS=0 '
        'ray start --disable-usage-stats --head '
        f'{ray_options};' + _RAY_PRLIMIT + _DUMP_RAY_PORTS,
        stream_logs=False,
        log_path=log_path_abs,
        require_outputs=True)
    if returncode:
        raise RuntimeError('Failed to start ray on the head node '
                           f'(exit code {returncode}). Error: '
                           f'===== stdout ===== \n{stdout}\n'
                           f'===== stderr ====={stderr}')


@_auto_retry
def start_ray_worker_nodes(cluster_name: str, no_restart: bool,
                           custom_resource: Optional[str],
                           cluster_metadata: common.ClusterMetadata,
                           ssh_credentials: Dict[str, str]) -> None:
    """Start Ray on the worker nodes."""
    if len(cluster_metadata.instances) <= 1:
        return

    ip_list = cluster_metadata.get_feasible_ips()
    ssh_runners = command_runner.SSHCommandRunner.make_runner_list(
        ip_list[1:], port_list=None, **ssh_credentials)
    worker_ids = [
        instance_id for instance_id in cluster_metadata.instances
        if instance_id != cluster_metadata.head_instance_id
    ]
    head_instance = cluster_metadata.get_head_instance()
    assert head_instance is not None, cluster_metadata
    head_private_ip = head_instance.private_ip

    ray_options = (
        f'--address={head_private_ip}:{constants.SKY_REMOTE_RAY_PORT} '
        f'--object-manager-port=8076 '
        f'--temp-dir={constants.SKY_REMOTE_RAY_TEMPDIR}')
    if custom_resource:
        ray_options += f' --resources=\'{custom_resource}\''

    cmd = (f'unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; '
           'RAY_SCHEDULER_EVENTS=0 RAY_DEDUP_LOGS=0 '
           f'ray start --disable-usage-stats {ray_options};' + _RAY_PRLIMIT)
    if no_restart:
        cmd = f'{RAY_STATUS_WITH_SKY_RAY_PORT_COMMAND} || ' + cmd
    else:
        cmd = 'ray stop; ' + cmd

    def _setup_ray_worker(runner_and_id: Tuple[command_runner.SSHCommandRunner,
                                               str]):
        # for cmd in config_from_yaml['worker_start_ray_commands']:
        #     cmd = cmd.replace('$RAY_HEAD_IP', ip_list[0][0])
        #     runner.run(cmd)
        runner, instance_id = runner_and_id
        log_dir = metadata_utils.get_instance_log_dir(cluster_name, instance_id)
        log_path_abs = str(log_dir / ('ray_cluster' + '.log'))
        return runner.run(cmd,
                          stream_logs=False,
                          require_outputs=True,
                          log_path=log_path_abs)

    results = subprocess_utils.run_in_parallel(
        _setup_ray_worker, list(zip(ssh_runners, worker_ids)))
    for returncode, stdout, stderr in results:
        if returncode:
            with ux_utils.print_exception_no_traceback():
                raise RuntimeError('Failed to start ray on the worker node '
                                   f'(exit code {returncode}). Error: '
                                   f'===== stdout ===== \n{stdout}\n'
                                   f'===== stderr ====={stderr}')


@_auto_retry
def start_skylet(cluster_name: str, cluster_metadata: common.ClusterMetadata,
                 ssh_credentials: Dict[str, str]) -> None:
    """Start skylet on the header node."""
    # "source ~/.bashrc" has side effects similar to
    # https://stackoverflow.com/questions/29709790/scripts-with-nohup-inside-dont-exit-correctly
    # This side effects blocks SSH from exiting. We address it by nesting
    # bash commands.
    ssh_runner = command_runner.SSHCommandRunner(
        cluster_metadata.get_feasible_ips()[0], port=22, **ssh_credentials)
    assert cluster_metadata.head_instance_id is not None, cluster_metadata
    log_dir = metadata_utils.get_instance_log_dir(
        cluster_name, cluster_metadata.head_instance_id)
    log_path_abs = str(log_dir / ('skylet' + '.log'))
    returncode, stdout, stderr = ssh_runner.run(_MAYBE_SKYLET_RESTART_CMD,
                                                stream_logs=False,
                                                require_outputs=True,
                                                log_path=log_path_abs)
    if returncode:
        raise RuntimeError('Failed to start skylet on the head node '
                           f'(exit code {returncode}). Error: '
                           f'===== stdout ===== \n{stdout}\n'
                           f'===== stderr ====={stderr}')


@_auto_retry
def _internal_file_mounts(file_mounts: Dict,
                          runner: command_runner.SSHCommandRunner,
                          log_path: str) -> None:
    if file_mounts is None or not file_mounts:
        return

    for dst, src in file_mounts.items():
        # TODO: We should use this trick to speed up file mounting:
        # https://stackoverflow.com/questions/1636889/how-can-i-configure-rsync-to-create-target-directory-on-remote-server
        full_src = os.path.abspath(os.path.expanduser(src))

        if os.path.isfile(full_src):
            mkdir_command = f'mkdir -p {os.path.dirname(dst)}'
        else:
            mkdir_command = f'mkdir -p {dst}'

        rc, stdout, stderr = runner.run(mkdir_command,
                                        log_path=log_path,
                                        stream_logs=False,
                                        require_outputs=True)
        subprocess_utils.handle_returncode(
            rc,
            mkdir_command, ('Failed to run command before rsync '
                            f'{src} -> {dst}.'),
            stderr=stdout + stderr)

        runner.rsync(
            source=src,
            target=dst,
            up=True,
            log_path=log_path,
            stream_logs=False,
        )


def internal_file_mounts(cluster_name: str, common_file_mounts: Dict,
                         cluster_metadata: common.ClusterMetadata,
                         ssh_credentials: Dict[str,
                                               str], wheel_hash: str) -> None:
    """Executes file mounts - rsyncing internal local files"""

    def _setup_node(runner: command_runner.SSHCommandRunner,
                    metadata: common.InstanceMetadata, log_path: str):
        del metadata
        _internal_file_mounts(common_file_mounts, runner, log_path)

    _parallel_ssh_with_cache(_setup_node,
                             cluster_name,
                             stage_name='internal_file_mounts',
                             digest=wheel_hash,
                             cluster_metadata=cluster_metadata,
                             ssh_credentials=ssh_credentials)
