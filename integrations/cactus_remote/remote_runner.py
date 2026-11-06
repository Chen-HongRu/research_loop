"""Fixed Cactus worker; RPC inputs contain parameters and identities, never commands."""

import base64
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROTOCOL = "tiny-cnn-1epoch-dev-v1"
INPUT_VERSION = "mle507f92e-mirror01a3db8-public-dev20-seed0-v1"
INPUT_METADATA = {
    "mlebench_commit": "507f92e1138bb6e40dac5c6ee7a6758e6424bf97",
    "source": "https://huggingface.co/datasets/TIGER-Lab/mle-bench/resolve/01a3db85f9245ba64d87ed2a1170d486ee147e1f/data/aerial-cactus-identification/prepared",
    "official_split": {"test_size": 0.19, "random_state": 0},
    "dev_split": {"test_size": 0.2, "random_state": 0, "stratify": None},
    "counts": {"train": 11340, "dev": 2835, "test": 3325},
}
LIMIT = 1_048_576
PAYLOAD_FIELDS = {
    "protocol",
    "authorization",
    "slot",
    "candidate",
    "params",
    "trainerVersion",
    "inputVersion",
    "evaluation",
    "evaluationVersion",
    "gpu",
    "expiresAt",
    "remoteDeadline",
    "jobSeconds",
    "executionIdentity",
    "remoteDeploymentIdentity",
}


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def deployment_identity(config):
    return hashlib.sha256(canonical(config).encode()).hexdigest()


def publish(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x") as output:
        output.write(canonical(value) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def regular_bytes(path, maximum=LIMIT):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Expected a bounded regular file")
        result = bytearray()
        while len(result) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        if len(result) > maximum:
            raise ValueError("Output exceeds size limit")
        return bytes(result)
    finally:
        os.close(descriptor)


def read_json(path):
    return json.loads(regular_bytes(path))


def deployment():
    root = Path(__file__).resolve().parent
    config = read_json(root / "deployment.json")
    if (
        set(config)
        != {
            "version",
            "remoteRoot",
            "remotePython",
            "remoteData",
            "remoteTrainer",
            "trainerVersion",
            "gpu",
        }
        or config["version"] != 1
    ):
        raise ValueError("Invalid fixed deployment")
    if Path(config["remoteRoot"]).resolve() != root:
        raise ValueError("Deployment root does not match installed runner")
    for name in ("remotePython", "remoteData", "remoteTrainer"):
        if not isinstance(config[name], str) or not Path(config[name]).is_absolute():
            raise ValueError("Deployment paths must be fixed absolute paths")
    return root, config


def validate(payload, config):
    if (
        not isinstance(payload, dict)
        or set(payload) != PAYLOAD_FIELDS
        or payload["protocol"] != PROTOCOL
    ):
        raise ValueError("Invalid fixed Cactus payload")
    params = payload["params"]
    if not isinstance(params, dict) or set(params) != {"learning_rate"}:
        raise ValueError("Only learning_rate may vary")
    lr = params["learning_rate"]
    if (
        isinstance(lr, bool)
        or not isinstance(lr, (float, int))
        or not math.isfinite(lr)
        or not 1e-5 <= lr <= 0.1
    ):
        raise ValueError("learning_rate must be finite and in [1e-5,0.1]")
    for key in PAYLOAD_FIELDS - {"params", "expiresAt", "remoteDeadline", "jobSeconds"}:
        if not isinstance(payload[key], str) or not payload[key]:
            raise ValueError("Missing immutable payload identity")
    for key in ("expiresAt", "remoteDeadline", "jobSeconds"):
        value = payload[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("Invalid original time authorization")
    if payload["jobSeconds"] > 1800:
        raise ValueError("Fixed Cactus jobs cannot exceed 30 minutes")
    if (
        payload["gpu"] != config["gpu"]
        or payload["trainerVersion"] != config["trainerVersion"]
        or payload["remoteDeploymentIdentity"] != deployment_identity(config)
        or payload["inputVersion"] != INPUT_VERSION
    ):
        raise ValueError("Payload differs from host-bound deployment")


def key_for(payload):
    return hashlib.sha256(
        json.dumps(
            [payload["authorization"], payload["slot"]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def process_identity(nonce):
    if Path("/proc/sys/kernel/random/boot_id").exists():
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        # Field 22 follows the parenthesized process name (which may contain spaces).
        start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    else:
        # Local macOS fixture support; production AutoDL uses Linux /proc above.
        boot = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True
        ).strip()
        start = subprocess.check_output(
            ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())], text=True
        ).strip()
    return {"boot_id": boot, "pid": os.getpid(), "starttime": start, "nonce": nonce}


def state(root, key, payload, collect=False):
    directory = root / "operations" / key
    claim_path = directory / "claim.json"
    if not claim_path.exists():
        return {"ok": True, "operationKey": key, "state": "not-submitted"}
    claim = read_json(claim_path)
    if claim["payload"] != payload:
        raise ValueError("Operation key is already bound to a different payload")
    answer = {
        "ok": True,
        "operationKey": key,
        "payload": claim["payload"],
        "state": "unknown",
    }
    job_path = directory / "job.json"
    if not job_path.exists():
        return answer
    job = read_json(job_path)
    if job["nonce"] != claim["nonce"] or job["jobRef"] != canonical(job["identity"]):
        raise ValueError("Original job identity mismatch")
    answer["job"] = job
    answer["state"] = (
        "last-confirmed-started" if time.time() < claim["deadline"] else "unknown"
    )
    if (directory / "result.json").exists():
        result = read_json(directory / "result.json")
        if (
            result["operationKey"] != key
            or result["jobRef"] != job["jobRef"]
            or result["payload"] != payload
        ):
            raise ValueError("Original result identity mismatch")
        answer.update(state=result["status"], result=result)
        if collect and result["status"] == "succeeded":
            content = regular_bytes(directory / "sealed-dev-predictions.csv")
            if len(content) != result["predictionBytes"]:
                raise ValueError("Sealed prediction metadata mismatch")
            answer["predictionsBase64"] = base64.b64encode(content).decode("ascii")
    return answer


def submit(root, config, key, payload):
    directory = root / "operations" / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (directory / "claim.json").exists():
        return state(root, key, payload)
    if time.time() * 1000 >= payload["remoteDeadline"]:
        raise ValueError("Original authorization expired; collection only")
    expected = config["trainerVersion"].removeprefix("sha256:")
    trainer_source = regular_bytes(config["remoteTrainer"], 65536)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected)
        or hashlib.sha256(trainer_source).hexdigest() != expected
    ):
        raise ValueError("Fixed trainer source version mismatch")
    metadata = json.loads(
        regular_bytes(Path(config["remoteData"]) / "metadata.json", 16384)
    )
    if any(metadata.get(key) != value for key, value in INPUT_METADATA.items()):
        raise ValueError("Pinned public Cactus input metadata mismatch")
    created = time.time()
    claim = {
        "payload": payload,
        "deployment": config,
        "trainerSourceBase64": base64.b64encode(trainer_source).decode("ascii"),
        "nonce": uuid.uuid4().hex,
        "createdAt": created,
        "deadline": min(
            payload["remoteDeadline"] / 1000, created + payload["jobSeconds"]
        ),
    }
    try:
        descriptor = os.open(
            directory / "claim.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w") as output:
            output.write(canonical(claim) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except FileExistsError:
        return state(root, key, payload)
    # Never erase a claim, including failure before Popen or before job.json.
    with (directory / "worker.log").open("ab") as log:
        subprocess.Popen(
            [
                config["remotePython"],
                "-u",
                str(Path(__file__).resolve()),
                "--worker",
                key,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    until = min(claim["deadline"], time.time() + 3)
    while not (directory / "job.json").exists() and time.time() < until:
        time.sleep(0.02)
    return state(root, key, payload)


def worker(key):
    root = Path(__file__).resolve().parent
    directory = root / "operations" / key
    claim = read_json(directory / "claim.json")
    config = claim["deployment"]
    payload = claim["payload"]
    validate(payload, config)
    if Path(config["remoteRoot"]).resolve() != root or key_for(payload) != key:
        raise ValueError("Frozen worker deployment/operation mismatch")
    trainer_source = base64.b64decode(claim["trainerSourceBase64"], validate=True)
    if len(trainer_source) > 65536 or hashlib.sha256(
        trainer_source
    ).hexdigest() != config["trainerVersion"].removeprefix("sha256:"):
        raise ValueError("Frozen trainer source mismatch")
    identity = process_identity(claim["nonce"])
    job_ref = canonical(identity)
    job = {"jobRef": job_ref, "identity": identity, "nonce": claim["nonce"]}
    # An internal duplicate worker cannot execute the trainer a second time.
    descriptor = os.open(
        directory / "job.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w") as output:
        output.write(canonical(job) + "\n")
    result = {
        "operationKey": key,
        "jobRef": job_ref,
        "payload": payload,
        "status": "failed",
        "exitCode": None,
        "startedAt": time.time(),
    }
    process = None
    try:
        trainer = directory / "fixed-train.py"
        with trainer.open("xb") as source:
            source.write(trainer_source)
        os.chmod(trainer, 0o400)
        seconds = min(payload["jobSeconds"], claim["deadline"] - time.time())
        if seconds <= 0:
            result["status"] = "timed_out"
            raise TimeoutError("Original deadline expired before trainer start")
        monotonic_deadline = time.monotonic() + seconds
        argv = [
            config["remotePython"],
            str(trainer),
            "--data",
            config["remoteData"],
            "--output",
            str(directory / "output"),
            "--lr",
            str(payload["params"]["learning_rate"]),
            "--epochs",
            "1",
            "--model",
            "tiny-cnn",
            "--seed",
            "0",
            "--batch-size",
            "128",
            "--threads",
            "4",
            "--prediction-splits",
            "dev",
            "--device",
            "cuda:0",
        ]
        environment = {
            "PATH": str(Path(config["remotePython"]).parent) + ":/usr/bin:/bin",
            "CUDA_VISIBLE_DEVICES": config["gpu"],
            "OMP_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        with (directory / "train.log").open("ab") as log:
            process = subprocess.Popen(
                argv,
                cwd=directory,
                env=environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                result["exitCode"] = process.wait(
                    timeout=max(0.001, monotonic_deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                result["status"] = "timed_out"
                os.killpg(process.pid, signal.SIGTERM)
                # Always KILL the original group after bounded grace, including
                # descendants ignoring TERM after the trainer itself exits.
                time.sleep(0.2)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                result["exitCode"] = process.wait()
                raise TimeoutError("Original remote job deadline reached")
        if result["exitCode"] != 0:
            raise ValueError("Fixed trainer exited unsuccessfully")
        content = regular_bytes(directory / "output" / "dev-predictions.csv")
        temporary = directory / "prediction.tmp"
        with temporary.open("xb") as output:
            output.write(content)
        os.chmod(temporary, 0o400)
        os.replace(temporary, directory / "sealed-dev-predictions.csv")
        result.update(status="succeeded", predictionBytes=len(content))
    except Exception as error:
        result["error"] = str(error)
    finally:
        result["finishedAt"] = time.time()
        publish(directory / "result.json", result)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        if not re.fullmatch(r"[0-9a-f]{64}", sys.argv[2]):
            raise ValueError("Invalid operation key")
        worker(sys.argv[2])
        return
    root = Path(__file__).resolve().parent
    request = json.loads(sys.stdin.buffer.read(65537))
    if request == {"action": "clock"}:
        print(canonical({"ok": True, "nowMs": time.time() * 1000}))
        return
    if set(request) != {"action", "operationKey", "payload"} or request[
        "action"
    ] not in {"submit", "query", "collect"}:
        raise ValueError("Invalid runner request")
    payload, key = request["payload"], request["operationKey"]
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", key)
        or key != key_for(payload)
    ):
        raise ValueError("Operation key does not match authorization and slot")
    claim_path = root / "operations" / key / "claim.json"
    if claim_path.exists():
        config = read_json(claim_path)["deployment"]
    else:
        _, config = deployment()
    validate(payload, config)
    answer = (
        submit(root, config, key, payload)
        if request["action"] == "submit"
        else state(root, key, payload, request["action"] == "collect")
    )
    print(canonical(answer))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(canonical({"ok": False, "error": str(error)}))
        sys.exit(1)
