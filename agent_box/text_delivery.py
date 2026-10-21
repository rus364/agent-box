"""Bounded, repeatable file delivery; stored texts never own agent lifecycle."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time

from .atomicio import atomic_write_bytes, atomic_write_json
from .config import effective_config
from .errors import AgentBoxError
from .paths import WORKFLOW_TEXTS
from .workflow_state import lock


PAGE_BYTES = 4096
METADATA_BYTES = 2048
IDENTIFIER = re.compile(r"[0-9a-f]{16}")


def check_size(size, settings, label="text", *, lower_bound=False):
    limit = settings.text_max_bytes
    if type(limit) is not int or limit < 1:
        raise AgentBoxError("workflow.text_max_bytes must be a positive integer")
    if size > limit:
        observed = f"at least {size}" if lower_bound else str(size)
        raise AgentBoxError(
            f"TEXT_LIMIT_EXCEEDED: {label}: {observed} UTF-8 bytes; "
            f"workflow.text_max_bytes={limit}. Stop this delivery and tell the user. "
            "Do not truncate, raise the limit or split the same text to bypass it; "
            "the user must decide whether to raise the limit or reduce the assignment's scope.")


def require_store(config):
    if not (config.workflow.enabled or config.tracker.enabled):
        raise AgentBoxError("Text delivery requires the workflow or tracker integration")
    policy = getattr(config, "_box_policy", None)
    if policy is not None and not policy["workflow_mount"]:
        raise AgentBoxError("Shared workflow storage is not mounted; stop/relaunch with workflow or tracker enabled")
    container = getattr(getattr(config, "_project_base", config), "_session_container", None)
    if container is None:
        container = Path("/run/.containerenv").exists()
    if container:
        # A rendered opt-in does not prove that an older box actually has its bind mount.
        mount = str(WORKFLOW_TEXTS.parent.resolve())
        try:
            entries = Path("/proc/self/mountinfo").read_text().splitlines()
            mounts = {re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])
                      for line in entries if len(line.split()) > 4}
        except OSError as exc:
            raise AgentBoxError(f"Cannot verify shared workflow storage: {exc}") from exc
        if mount not in mounts:
            raise AgentBoxError("Shared workflow storage is not mounted; stop/relaunch the workspace")
    if WORKFLOW_TEXTS.is_symlink():
        raise AgentBoxError("Text storage directory is a symlink; leaving it untouched")


def chunks(raw, size):
    if type(size) is not int or not 256 <= size <= PAGE_BYTES:
        raise AgentBoxError(f"Text page size must be between 256 and {PAGE_BYTES} bytes")
    if not raw:
        yield 0, b""
    offset = 0
    while offset < len(raw):
        end = min(len(raw), offset + size)
        while end < len(raw) and raw[end] & 0xc0 == 0x80:
            end -= 1
        yield offset, raw[offset:end]
        offset = end


def page_name(index):
    return f"page-{index:06d}.txt"


def page_files(directory, raw, size):
    parts = list(chunks(raw, size))
    for index, (offset, part) in enumerate(parts, 1):
        end = offset + len(part)
        header = f"agent-box text {directory.name}: UTF-8 bytes {offset}..{end} of {len(raw)}\n--- TEXT ---\n"
        # Sibling paths stay valid when the existing bind mount has a different destination.
        footer = (f"\n--- END PAGE ---\nNext: read sibling {json.dumps(page_name(index + 1))}\n"
                  if index < len(parts) else f"\n--- END PAGE ---\nEND OF TEXT {directory.name}: {len(raw)} bytes\n")
        wrapper = (header + footer).encode()
        if len(wrapper) > METADATA_BYTES:
            raise AgentBoxError("Text page metadata is too large")
        yield page_name(index), header.encode() + part + footer.encode()


def descriptor(directory, metadata):
    return {"id": directory.name, "bytes": metadata["bytes"], "sha256": metadata["sha256"],
            "pages": metadata["pages"], "path": str(directory / page_name(1)),
            "expires_at": metadata["expires_at"]}


def metadata_at(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise AgentBoxError("Text record is missing or is not an owned directory")
    path = directory / "record.json"
    if path.is_symlink():
        raise AgentBoxError("Text metadata is a symlink; leaving it untouched")
    with path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise AgentBoxError("Text metadata is too large")
    metadata = json.loads(raw)
    if not isinstance(metadata, dict) or metadata.get("kind") != "agent-box-text":
        raise AgentBoxError("Text record is not owned by agent-box")
    if type(metadata.get("format_version")) is not int or metadata["format_version"] != 1:
        raise AgentBoxError("Unsupported text record format; preserve it and use a compatible runtime")
    if (metadata.get("id") != directory.name
            or any(type(metadata.get(key)) is not int or metadata[key] < 0 for key in ("bytes", "pages"))
            or not 1 <= metadata["pages"] <= max(1, metadata["bytes"])
            or type(metadata.get("page_bytes")) is not int or not 256 <= metadata["page_bytes"] <= PAGE_BYTES
            or any(type(metadata.get(key)) not in (int, float) or not math.isfinite(metadata[key])
                   for key in ("created_at", "expires_at"))
            or metadata["expires_at"] < metadata["created_at"]
            or not isinstance(metadata.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])):
        raise AgentBoxError("Invalid text record metadata")
    return metadata


def cleanup(now=None):
    """Only complete, recognized records are ours to prune; surprises survive."""
    now = time.time() if now is None else now
    if not WORKFLOW_TEXTS.exists():
        return
    for directory in WORKFLOW_TEXTS.iterdir():
        if not IDENTIFIER.fullmatch(directory.name):
            continue
        try:
            metadata = metadata_at(directory)
            if metadata["expires_at"] > now:
                continue
            entries = list(directory.iterdir())
            def owned(path):
                page = re.fullmatch(r"page-(\d{6,}).txt", path.name)
                return (path.name in {"record.json", "source.txt"}
                        or page and 1 <= int(page[1]) <= metadata["pages"] and path.name == page_name(int(page[1])))
            if any(not owned(p) or not stat.S_ISREG(p.lstat().st_mode) for p in entries):
                continue
            for path in entries:
                path.unlink(missing_ok=True)
            directory.rmdir()
        except (OSError, AgentBoxError, ValueError):
            continue


def prepare(config):
    """Coordinator preparation prunes old deliveries even when this run uses stdin."""
    if WORKFLOW_TEXTS.exists():
        require_store(config)
        # The shared store may have different host/container paths; its lock key must not.
        with lock("/", "texts", blocking=True):
            cleanup()


def publish(text, config, *, page_bytes=PAGE_BYTES):
    raw = text.encode("utf-8")
    check_size(len(raw), config.workflow)
    days = config.workflow.text_retention_days
    if type(days) is not int or days < 1:
        raise AgentBoxError("workflow.text_retention_days must be a positive integer")
    require_store(config)
    # Validate paging before creating any files, even for an empty record.
    list(chunks(raw, page_bytes))
    WORKFLOW_TEXTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock("/", "texts", blocking=True):
        cleanup()
        for _ in range(32):
            ident = secrets.token_hex(8)
            directory = WORKFLOW_TEXTS / ident
            if not directory.exists() and not directory.is_symlink():
                break
        else:
            raise AgentBoxError("Could not allocate a unique text ID")
        pages = list(page_files(directory, raw, page_bytes))
        now = time.time()
        metadata = {"format_version": 1, "kind": "agent-box-text", "id": ident,
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "created_at": now, "expires_at": now + days * 86400,
                    "page_bytes": page_bytes, "pages": len(pages)}
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=WORKFLOW_TEXTS))
        try:
            atomic_write_bytes(pending / "source.txt", raw, 0o400)
            for name, content in pages:
                atomic_write_bytes(pending / name, content, 0o400)
            atomic_write_json(pending / "record.json", metadata, 0o400)
            os.rename(pending, directory)
        finally:
            if pending.exists():
                shutil.rmtree(pending)
        return descriptor(directory, metadata)


def locate(ident, config):
    """Issue a checked locator without locks, writes or cleanup from a read-only role."""
    if not IDENTIFIER.fullmatch(ident):
        raise AgentBoxError("Invalid text ID: expected 16 lowercase hexadecimal characters")
    require_store(config)
    directory = WORKFLOW_TEXTS / ident
    try:
        metadata = metadata_at(directory)
        if metadata["expires_at"] <= time.time():
            raise AgentBoxError(f"Text {ident} has expired; request a new handoff")
        check_size(metadata["bytes"], config.workflow, f"text {ident}")
        source = directory / "source.txt"
        if source.is_symlink() or not source.is_file():
            raise AgentBoxError(f"Text {ident} is missing or is not a regular file")
        with source.open("rb") as stream:
            raw = stream.read(config.workflow.text_max_bytes + 1)
        check_size(len(raw), config.workflow, f"text {ident}", lower_bound=True)
        raw.decode("utf-8")
        if len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise AgentBoxError(f"Text {ident} changed; request a new handoff")
        pages = list(page_files(directory, raw, metadata["page_bytes"]))
        if len(pages) != metadata["pages"]:
            raise AgentBoxError(f"Text {ident} has inconsistent pages")
        for name, expected in pages:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise AgentBoxError(f"Text {ident} has missing or invalid pages")
            with path.open("rb") as stream:
                if stream.read(len(expected) + 1) != expected:
                    raise AgentBoxError(f"Text {ident} has changed or incomplete pages")
        return descriptor(directory, metadata)
    except (OSError, ValueError) as exc:
        raise AgentBoxError(f"Cannot read text {ident}: {exc}") from exc


def message(artifact):
    return (f"Read text {artifact['id']} ({artifact['bytes']} UTF-8 bytes, {artifact['pages']} pages), "
            f"starting at {json.dumps(artifact['path'])}. Use your available file-reading tool, one page per call; "
            "resolve each next sibling path in the current page's directory and continue through END OF TEXT. "
            "Page boundaries do not remove any requirements. "
            "Do not infer completeness from line counts, combine pages into one tool output, or reopen the original "
            "through an unbounded read. On missing, expired, changed or truncated content, stop and report the "
            "incomplete delivery to the coordinator. The coordinator must inform the user of size-limit failures; "
            "never raise the limit or split a refused text to bypass it without the user's decision.")


def parser():
    p = argparse.ArgumentParser(prog="agent-box text", description="Temporary UTF-8 pages in shared workflow storage")
    sub = p.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="Read UTF-8 stdin and return an ID and generated page path; never a path argument")
    write.add_argument("--page-bytes", type=int, default=PAGE_BYTES,
                       help=f"Content bytes per page, 256..{PAGE_BYTES} (default {PAGE_BYTES})")
    locate_parser = sub.add_parser("locate", help="Check an ID and return its page path; no content, deletion or writes")
    locate_parser.add_argument("id", help="ID returned by text write or a workflow handoff")
    return p


def main(argv):
    try:
        args = parser().parse_args(argv)
        config = effective_config()
        if args.command == "write":
            check_size(0, config.workflow)
            raw = sys.stdin.buffer.read(config.workflow.text_max_bytes + 1)
            check_size(len(raw), config.workflow, lower_bound=True)
            result = publish(raw.decode("utf-8"), config, page_bytes=args.page_bytes)
        else:
            result = locate(args.id, config)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AgentBoxError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
