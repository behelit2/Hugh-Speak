#!/usr/bin/python3
# HUGH_SPEAK_STABLE_TERMINAL_V1
"""
hugh_speak.py

Run this client from your home directory.

Launch Hugh locally or through SSH and speak final-answer output through
the client computer's speakers.

This file contains both the local client and a temporary backend adapter.
The adapter is supplied to Hugh's Python interpreter at launch. It does
not modify hugh_granite.py or granite.env.

Commands and conversation transactions remain Hugh's responsibility.

Client-specific behavior:
  * Speech and prompt-editing history are local.
  * Begin typing at the prompt to stop remaining speech.
  * Ctrl+C at the prompt delegates removal/exit to Hugh.
  * Ctrl+C during work interrupts Hugh and stops speech.
  * Ctrl+Q disconnects the client if normal interruption is unavailable.

Run model-free tests:
    /usr/bin/python3 ~/hugh_speak.py --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import sys
from pathlib import Path


# =====================================================================
# USER SETTINGS
# =====================================================================

# ---------------------------------------------------------------------
# 1. Choose how Hugh is launched
#
# "local" = Hugh runs on this computer.
# "ssh"   = Hugh runs on another computer through SSH.
#
# Speech ALWAYS plays on this computer.
# ---------------------------------------------------------------------

CONNECTION_MODE = "local"


# ---------------------------------------------------------------------
# 2. LOCAL settings
# ---------------------------------------------------------------------

LOCAL_HUGH_DIRECTORY = "~/models/granite-4.2-8b"
LOCAL_PYTHON = "~/.venvs/hugh/bin/python3"


# ---------------------------------------------------------------------
# 3. SSH settings
#
# These paths are evaluated ON THE SERVER.
# ---------------------------------------------------------------------

SSH_HOST = "user@0.0.0.0"
SSH_HUGH_DIRECTORY = "~/models/granite-4.2-8b"
SSH_PYTHON = "~/.venvs/hugh/bin/python3"


# ---------------------------------------------------------------------
# 4. Hugh filenames
#
# Relative to the selected Hugh directory unless absolute.
# ---------------------------------------------------------------------

HUGH_PROGRAM = "hugh_granite.py"
HUGH_ENVIRONMENT = "granite.env"


# ---------------------------------------------------------------------
# 5. Local voice settings
#
# List English voices:
#   espeak-ng --voices=en
# ---------------------------------------------------------------------

VOICE = "en-gb+m7+klatt3"
PITCH = 38
WORDS_PER_MINUTE = 155


# ---------------------------------------------------------------------
# 6. Speech responsiveness
#
# Natural sentence boundaries can start speech immediately.
#
# If a sentence is not complete, the timer attempts to release text at
# a whitespace boundary. It never intentionally cuts an unfinished word.
#
# These are collection delays, not guarantees of audible startup time.
# ---------------------------------------------------------------------

SPEECH_START_DELAY = 0.30
SPEECH_CONTINUE_DELAY = 0.65

FIRST_CHUNK_MAX_CHARS = 90
LATER_CHUNK_MAX_CHARS = 90


# ---------------------------------------------------------------------
# 7. Local prompt-editing history
#
# PROMPT_HISTORY_ENABLED from Hugh's environment controls whether this
# client persists its own editing history.
#
# In SSH mode this is a CLIENT path, not a server path.
#
# /history-clear also clears this local history after Hugh successfully
# handles the command.
# ---------------------------------------------------------------------

LOCAL_HISTORY_FILE = "~/.local/state/hugh_speak/prompt_history"


# =====================================================================
# END OF USER SETTINGS
# =====================================================================


WIRE_PREFIX = "@@HUGH_SPEAK/1@@"
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_INPUT_BYTES = 8 * 1024 * 1024

HISTORY_EXCLUDED = {
    "/help",
    "/clear",
    "/history-clear",
    "/quit",
}


try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.input.defaults import create_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output.defaults import create_output
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:
    print(
        "Missing prompt_toolkit.\n"
        "Install it with:\n"
        "  sudo dnf install python3-prompt-toolkit\n\n"
        "Run this client with:\n"
        "  /usr/bin/python3 ~/hugh_speak.py",
        file=sys.stderr,
    )
    raise SystemExit(1)


# =====================================================================
# Temporary backend adapter
#
# This source is passed to the selected backend Python interpreter using
# -c. It imports the existing Hugh file and adapts its terminal boundary
# in memory. The existing chat loop still owns all conversation changes.
#
# Input and control messages use JSON lines.
# Output uses explicitly framed JSON events.
# =====================================================================

BACKEND_BRIDGE = r'''
import builtins
import contextlib
import importlib.util
import json
import os
import queue
import signal
import sys
import threading

PREFIX = "@@HUGH_SPEAK/1@@"
MAX_INPUT = 8 * 1024 * 1024

original_stdout = sys.stdout
original_stderr = sys.stderr

wire_lock = threading.RLock()
state_lock = threading.RLock()
inbox = queue.Queue()
disconnected = threading.Event()
thread_context = threading.local()

state = {
    "phase": "startup",
    "operation": 0,
    "prompt_sequence": 0,
    "interrupted": None,
    "pending_cancel": None,
    "pending_signal": None,
}

response_sequence = 0
module = None
editor_created = False


def operation():
    return state["operation"]


def signal_main():
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(os.getpid(), signal.SIGINT)


def disconnect():
    first = not disconnected.is_set()
    disconnected.set()
    inbox.put(("eof", None, None))

    if first:
        with state_lock:
            active = state["phase"] in {"startup", "working"}

        if active:
            signal_main()


def emit(event, **values):
    if disconnected.is_set():
        return

    record = {"event": event, **values}
    line = PREFIX + json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
    ) + "\n"

    try:
        with wire_lock:
            original_stdout.write(line)
            original_stdout.flush()
    except (BrokenPipeError, OSError):
        disconnect()


class Output:
    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)

        if not text:
            return 0

        if getattr(thread_context, "answer", False):
            emit(
                "answer_delta",
                operation=getattr(
                    thread_context, "operation", operation()
                ),
                response=getattr(thread_context, "response", 0),
                text=text,
            )
        else:
            emit("text", text=text)

        return len(text)

    def flush(self):
        return None

    def isatty(self):
        return False

    def writable(self):
        return True

    def fileno(self):
        return original_stdout.fileno()


def on_interrupt(signum, frame):
    with state_lock:
        phase = state["phase"]
        current = state["operation"]
        pending = state["pending_signal"]

        if disconnected.is_set():
            if phase in {"startup", "working"}:
                state["phase"] = "recovering"
                raise KeyboardInterrupt
            return

        # An old cancellation must not become a prompt-time removal.
        if pending != current:
            return

        state["pending_signal"] = None

        if phase in {"startup", "working"}:
            state["phase"] = "recovering"
            raise KeyboardInterrupt


def receive():
    try:
        while True:
            raw = sys.stdin.buffer.readline(MAX_INPUT + 1)

            if not raw:
                disconnect()
                return

            if len(raw) > MAX_INPUT or not raw.endswith(b"\n"):
                raise ValueError("Oversized or incomplete input frame.")

            value = json.loads(raw.decode("utf-8"))

            if not isinstance(value, dict):
                raise ValueError("Input frame must be an object.")

            action = value.get("op")
            identifier = value.get("id")

            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or identifier < 0
            ):
                raise ValueError("Invalid operation identifier.")

            send_signal = False

            with state_lock:
                if identifier != state["operation"]:
                    continue

                phase = state["phase"]

                if action == "input":
                    text = value.get("text")

                    if not isinstance(text, str):
                        raise ValueError("Input text must be a string.")

                    if phase != "prompt":
                        continue

                    # Do not accept a second submission for this prompt.
                    state["phase"] = "queued"
                    inbox.put(("input", identifier, text))

                elif action == "interrupt":
                    if state["interrupted"] == identifier:
                        continue

                    state["interrupted"] = identifier

                    if phase == "prompt":
                        state["phase"] = "queued_control"
                        inbox.put(("interrupt", identifier, None))

                    elif phase in {"queued", "submitted"}:
                        # parse_request is inside Hugh's protected
                        # work transaction. Deliver cancellation there.
                        state["pending_cancel"] = identifier

                    elif phase in {"startup", "working"}:
                        state["pending_signal"] = identifier
                        send_signal = True

                else:
                    raise ValueError("Unknown client operation.")

            if send_signal:
                signal_main()

    except Exception as exc:
        with contextlib.suppress(Exception):
            original_stderr.write(
                "\n[Hugh Speak bridge input error: "
                + type(exc).__name__ + ": " + str(exc) + "]\n"
            )
            original_stderr.flush()
        disconnect()


def settings(config, reset_history=False):
    emit(
        "settings",
        history_enabled=bool(config.PROMPT_HISTORY_ENABLED),
        multiline=bool(config.PROMPT_MULTILINE),
        toolbar=bool(config.PROMPT_BOTTOM_TOOLBAR),
        reset_history=bool(reset_history),
    )


def make_editor(config):
    global editor_created

    # In the supplied Hugh version, create_editor is called initially
    # and after a successful /history-clear.
    settings(config, reset_history=editor_created)
    editor_created = True

    return module.PromptEditor(session=None, persistent=False)


def read_input(editor, config):
    if disconnected.is_set():
        raise EOFError

    with state_lock:
        state["prompt_sequence"] += 1
        identifier = state["prompt_sequence"]
        state["operation"] = identifier
        state["phase"] = "prompt"
        state["interrupted"] = None
        state["pending_cancel"] = None
        state["pending_signal"] = None

    emit("prompt", id=identifier)

    while True:
        action, target, text = inbox.get()

        if action == "eof" or disconnected.is_set():
            raise EOFError

        if target != identifier:
            continue

        if action == "interrupt":
            with state_lock:
                state["phase"] = "recovering"
            raise KeyboardInterrupt

        if action == "input":
            with state_lock:
                state["phase"] = "submitted"
            return text


def redraw(messages, notice=""):
    transcript = []

    for message in messages:
        role = message.get("role")

        if role not in {"user", "assistant"}:
            continue

        transcript.append({
            "role": role,
            "text": str(
                message.get(
                    "display_content",
                    message.get("content", ""),
                )
            ),
        })

    emit(
        "redraw",
        header=str(getattr(module, "HEADER", "")),
        messages=transcript,
        notice=str(notice),
    )


def clear_screen():
    emit("clear")


def begin_answer():
    global response_sequence

    response_sequence += 1
    identifier = response_sequence
    current = operation()

    emit(
        "answer_begin",
        operation=current,
        response=identifier,
    )

    return current, identifier


def end_answer(current, identifier, aborted=False):
    emit(
        "answer_abort" if aborted else "answer_end",
        operation=current,
        response=identifier,
    )


def install():
    original_parse = module.parse_request
    original_chat = module.chat_main
    original_generate = module.GraniteEngine.generate

    def parse_request(*args, **kwargs):
        with state_lock:
            current = state["operation"]
            state["phase"] = "working"
            cancelled = state["pending_cancel"] == current
            state["pending_cancel"] = None

            if cancelled or disconnected.is_set():
                state["phase"] = "recovering"

        emit("working", id=current)

        if cancelled or disconnected.is_set():
            raise KeyboardInterrupt

        return original_parse(*args, **kwargs)

    def chat_main(config):
        # Send editor configuration before model loading so the local
        # editor can own keyboard handling during startup.
        settings(config)
        return original_chat(config)

    def generate(self, *args, **kwargs):
        if not kwargs.get("stream", False):
            return original_generate(self, *args, **kwargs)

        current, identifier = begin_answer()
        thread_context.answer = True
        thread_context.operation = current
        thread_context.response = identifier

        try:
            result = original_generate(self, *args, **kwargs)
        except BaseException:
            end_answer(current, identifier, aborted=True)
            raise
        else:
            end_answer(current, identifier)
            return result
        finally:
            thread_context.answer = False

    heading = "\n===\nHugh: "

    def module_print(*args, **kwargs):
        # These are application print CALLS, not strings discovered in
        # an output stream. Streamed model text uses stdout.write and
        # therefore cannot accidentally invoke this heading handler.
        target = kwargs.get("file")

        if (
            len(args) == 1
            and isinstance(args[0], str)
            and target in (None, sys.stdout)
            and args[0].startswith(heading)
            and len(args[0]) > len(heading)
        ):
            text = args[0][len(heading):]
            ending = kwargs.get("end", "\n")

            if ending is None:
                ending = "\n"

            sys.stdout.write(heading)
            current, identifier = begin_answer()

            emit(
                "answer_delta",
                operation=current,
                response=identifier,
                text=text + ending,
            )
            end_answer(current, identifier)
            return

        return builtins.print(*args, **kwargs)

    module.create_editor = make_editor
    module.read_input = read_input
    module.redraw = redraw
    module.clear_screen = clear_screen
    module.parse_request = parse_request
    module.chat_main = chat_main
    module.GraniteEngine.generate = generate
    module.print = module_print


def main():
    global module

    if len(sys.argv) != 2:
        raise RuntimeError("The bridge needs one Hugh program path.")

    path = os.path.abspath(os.path.expanduser(sys.argv[1]))
    sys.path.insert(0, os.path.dirname(path))
    sys.argv = [path]

    spec = importlib.util.spec_from_file_location(
        "_hugh_speak_backend", path
    )

    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the configured Hugh program.")

    module = importlib.util.module_from_spec(spec)

    # Registration is important for dataclasses and normal module
    # introspection during import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    required = (
        "PromptEditor",
        "create_editor",
        "read_input",
        "redraw",
        "clear_screen",
        "parse_request",
        "chat_main",
        "GraniteEngine",
        "main",
    )

    missing = [name for name in required if not hasattr(module, name)]

    if missing:
        raise RuntimeError(
            "This Hugh version lacks adapter interfaces: "
            + ", ".join(missing)
        )

    if not hasattr(module.GraniteEngine, "generate"):
        raise RuntimeError("GraniteEngine.generate is unavailable.")

    install()
    return module.main()


signal.signal(signal.SIGINT, on_interrupt)
sys.stdout = Output()

threading.Thread(
    target=receive,
    name="hugh-speak-controls",
    daemon=True,
).start()

emit("hello", version=1)

status = 1

try:
    status = main()
    status = 0 if status is None else int(status)
except KeyboardInterrupt:
    status = 130
    emit("text", text="\n[Hugh startup interrupted.]\n")
except Exception as exc:
    with contextlib.suppress(Exception):
        original_stderr.write(
            "\n[Hugh Speak backend error: "
            + type(exc).__name__ + ": " + str(exc) + "]\n"
        )
        original_stderr.flush()
    status = 1
finally:
    emit("exit", status=status)

raise SystemExit(status)
'''


# =====================================================================
# Launching and framing
# =====================================================================

def shell_path(value: str) -> str:
    """Quote a path while evaluating ~ on Hugh's machine."""
    if value == "~":
        return '"$HOME"'

    if value.startswith("~/"):
        return '"$HOME"/' + shlex.quote(value[2:])

    return shlex.quote(value)


def build_launch_command(mode: str) -> list[str]:
    if mode == "local":
        directory = LOCAL_HUGH_DIRECTORY
        python = LOCAL_PYTHON
    else:
        directory = SSH_HUGH_DIRECTORY
        python = SSH_PYTHON

    environment = HUGH_ENVIRONMENT

    if not environment.startswith(("/", "~", "./", "../")):
        environment = "./" + environment

    command = "\n".join(
        [
            "set -e",
            "cd -- " + shell_path(directory),
            "source " + shell_path(environment),
            # Hide reasoning in this launched session. This does not
            # change the requested thinking mode or its computation.
            "export SHOW_THINKING=0",
            "export PYTHONUNBUFFERED=1",
            "export PYTHONIOENCODING=utf-8",
            "exec "
            + shell_path(python)
            + " -u -c "
            + shlex.quote(BACKEND_BRIDGE)
            + " "
            + shell_path(HUGH_PROGRAM),
        ]
    )

    if mode == "ssh":
        remote_command = "exec bash -lc " + shlex.quote(command)

        return [
            "ssh",
            "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            SSH_HOST.strip(),
            remote_command,
        ]

    return ["bash", "-lc", command]


def decode_wire_line(line: str):
    """
    Return (unframed_prefix, event).

    Shell startup output may precede the adapter handshake. It is
    displayed as diagnostics, never interpreted as answer text.
    """
    position = line.find(WIRE_PREFIX)

    if position < 0:
        return line, None

    prefix = line[:position]
    payload = line[position + len(WIRE_PREFIX):].rstrip("\r\n")

    try:
        value = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Invalid backend protocol frame.") from exc

    if (
        not isinstance(value, dict)
        or not isinstance(value.get("event"), str)
    ):
        raise RuntimeError("Invalid backend protocol event.")

    return prefix, value


async def decoded_chunks(stream):
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    while True:
        data = await stream.read(4096)
        text = decoder.decode(data, final=not data)

        if text:
            yield text

        if not data:
            return


# =====================================================================
# Speech
# =====================================================================

def clean_speech_text(text: str) -> str:
    """Lightweight Markdown cleanup for speech, not displayed output."""
    text = re.sub(
        r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)",
        "",
        text,
    )
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        " ",
        text,
    )

    text = re.sub(
        r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$",
        "",
        text,
    )
    text = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(
        r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$",
        "",
        text,
    )

    text = text.replace("*", "").replace("`", "")
    text = re.sub(r"(?<!\w)_{1,2}(?=\S)", "", text)
    text = re.sub(r"(?<=\S)_{1,2}(?!\w)", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:>[ \t]*)+", "", text)
    text = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", text)
    text = re.sub(r"(?m)^[ \t]*[-+•][ \t]+", "", text)
    text = re.sub(r"(?m)^[ \t]*\d+[.)][ \t]+", "", text)

    return " ".join(text.split())


class Speech:
    """Collect short, responsive chunks and play them serially."""

    SENTENCE_END = re.compile(
        r"""[.!?]["'”’)*_`]*[ \t\r\n]+|\n+"""
    )
    CLAUSE_END = re.compile(
        r"""[,;:]["'”’)*_`]*[ \t]+"""
    )

    def __init__(self, executable: str):
        self.executable = executable
        self.queue = asyncio.Queue()
        self.generation = 0
        self.pending = ""
        self.enabled = False
        self.first_chunk = True
        self.timer = None
        self.process = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(
            self._worker(), name="local-speech"
        )

    def _cancel_timer(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    @staticmethod
    def _kill_if_running(process):
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    @classmethod
    def _terminate(cls, process):
        if process.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError):
            process.terminate()

        asyncio.get_running_loop().call_later(
            0.3, cls._kill_if_running, process
        )

    def interrupt(self):
        """
        Stop audio and mute the current response.

        Late feed() calls cannot resume speech. A genuinely new
        answer_begin event is required to enable it again.
        """
        self.generation += 1
        self.enabled = False
        self.pending = ""
        self._cancel_timer()

        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        if self.process is not None:
            self._terminate(self.process)

    def begin_response(self, enabled=True):
        self.interrupt()
        self.enabled = bool(enabled)
        self.first_chunk = True

    def _enqueue(self, text: str) -> bool:
        text = clean_speech_text(text)

        if not text:
            return False

        self.queue.put_nowait((self.generation, text))
        return True

    def _take(self, cut: int):
        text = self.pending[:cut]
        self.pending = self.pending[cut:]

        if self._enqueue(text):
            self.first_chunk = False

    def _limit(self):
        return LATER_CHUNK_MAX_CHARS

    def _word_cut(self, maximum: int) -> int:
        matches = list(
            re.finditer(r"\s+", self.pending[:maximum])
        )
        return matches[-1].end() if matches else 0

    def _natural_cut(self, maximum: int) -> int:
        candidates = []

        for match in self.SENTENCE_END.finditer(
            self.pending, 0, maximum
        ):
            if match.end() < 100:
                continue

            candidate = self.pending[:match.end()]

            if clean_speech_text(candidate):
                candidates.append(match.end())

        return candidates[-1] if candidates else 0

    def _drain_available(self):
        while self.enabled and self.pending:
            maximum = self._limit()
            cut = self._natural_cut(maximum)

            if not cut and len(self.pending) >= maximum:
                cut = self._word_cut(maximum)

            if not cut:
                break

            self._take(cut)

    def _arm_timer(self):
        # Stability mode: do not turn an incomplete sentence into a
        # succession of small timed utterances. end_response() still
        # flushes short answers and the final remainder.
        self._cancel_timer()

    def _flush_due(self, generation):
        self.timer = None

        if generation != self.generation or not self.enabled:
            return

        cut = self._word_cut(self._limit())

        if cut:
            self._take(cut)
            self._drain_available()

        self._arm_timer()

    def feed(self, text: str):
        if not self.enabled or not text:
            return

        self.pending += text
        self._drain_available()

        if not self.pending:
            self._cancel_timer()
        else:
            self._arm_timer()

    def end_response(self):
        self._cancel_timer()

        if self.enabled and self.pending:
            self._enqueue(self.pending)

        self.pending = ""
        self.enabled = False

    async def _worker(self):
        while True:
            item = await self.queue.get()

            if item is None:
                return

            generation, text = item

            if generation != self.generation:
                continue

            process = None

            try:
                process = await asyncio.create_subprocess_exec(
                    self.executable,
                    "-v", VOICE,
                    "-p", str(PITCH),
                    "-s", str(WORDS_PER_MINUTE),
                    "-z",
                    "--stdin",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.process = process

                if generation != self.generation:
                    self._terminate(process)

                _, error = await process.communicate(
                    input=text.encode("utf-8")
                )

                if (
                    process.returncode != 0
                    and generation == self.generation
                ):
                    detail = error.decode(
                        "utf-8", errors="replace"
                    ).strip()

                    print(
                        "\n[Voice error: "
                        + (detail[-500:] or str(process.returncode))
                        + "]",
                        file=sys.stderr,
                        flush=True,
                    )
                    self.interrupt()

            except asyncio.CancelledError:
                if process is not None:
                    self._terminate(process)

                    with contextlib.suppress(Exception):
                        await process.wait()
                raise

            except (OSError, TypeError, ValueError) as exc:
                print(
                    f"\n[Cannot run speech engine: {exc}]",
                    file=sys.stderr,
                    flush=True,
                )
                self.interrupt()

            finally:
                if process is not None and process.returncode is None:
                    self._terminate(process)

                    with contextlib.suppress(Exception):
                        await process.wait()

                if self.process is process:
                    self.process = None

    async def close(self):
        self.interrupt()

        if self.task is None:
            return

        if not self.task.done():
            self.queue.put_nowait(None)

            try:
                await asyncio.wait_for(
                    asyncio.shield(self.task), timeout=2
                )
            except asyncio.TimeoutError:
                self.task.cancel()

        await asyncio.gather(self.task, return_exceptions=True)


# =====================================================================
# Local prompt editor
# =====================================================================

class FilteredFileHistory(FileHistory):
    def append_string(self, value):
        if (
            value.strip()
            and value.strip().lower() not in HISTORY_EXCLUDED
        ):
            super().append_string(value)


class FilteredMemoryHistory(InMemoryHistory):
    def append_string(self, value):
        if (
            value.strip()
            and value.strip().lower() not in HISTORY_EXCLUDED
        ):
            super().append_string(value)


def prepare_history_file(clear=False) -> str:
    path = Path(LOCAL_HISTORY_FILE).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)

    descriptor = os.open(path, flags, 0o600)

    try:
        info = os.fstat(descriptor)

        if not stat.S_ISREG(info.st_mode):
            raise OSError("Local editing history is not a regular file.")

        if info.st_uid != os.getuid():
            raise OSError("Local editing history is owned by another user.")

        os.fchmod(descriptor, 0o600)

        if clear:
            os.ftruncate(descriptor, 0)
    finally:
        os.close(descriptor)

    return str(path)


class Editor:
    """
    Keep one editor active during work as well as input.

    During work its buffer is read-only, but Ctrl+C still works. Keeping
    terminal input in raw mode also prevents a typed Ctrl+C from killing
    the SSH client instead of becoming an explicit backend control event.
    """

    def __init__(self, speech: Speech, outgoing: asyncio.Queue):
        self.speech = speech
        self.outgoing = outgoing
        self.available = asyncio.Event()
        self.force_close = asyncio.Event()

        self.settings = {
            "history_enabled": False,
            "multiline": True,
            "toolbar": True,
        }

        self.history = FilteredMemoryHistory()
        self.session = None
        self.stopping = False
        self.phase = "startup"
        self.operation = 0
        self.cancelled_operations = set()
        self.interrupt_requested = None
        self.typing_stopped_speech = False

    def invalidate(self):
        changed = getattr(self, "_state_changed", None)

        if changed is not None:
            changed.set()

        if self.session is not None:
            self.session.app.invalidate()

    def _rebuild(self):
        changed = getattr(self, "_state_changed", None)

        if changed is not None:
            changed.set()

        if self.session is None:
            return

        app = self.session.app
        future = getattr(app, "future", None)

        if (
            app.is_running
            and future is not None
            and not future.done()
        ):
            app.exit(result=None)

    def configure(self, event):
        new_settings = {
            name: bool(event.get(name, self.settings[name]))
            for name in self.settings
        }
        first = not self.available.is_set()
        reset = bool(event.get("reset_history"))
        changed = new_settings != self.settings
        history_changed = (
            new_settings["history_enabled"]
            != self.settings["history_enabled"]
        )

        self.settings = new_settings

        if first or reset or history_changed:
            if self.settings["history_enabled"]:
                try:
                    path = prepare_history_file(clear=reset)
                    self.history = FilteredFileHistory(path)
                except OSError as exc:
                    self.history = FilteredMemoryHistory()
                    print(
                        "\n[Using in-memory local editing history: "
                        + str(exc) + "]",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                self.history = FilteredMemoryHistory()

        self.available.set()

        if reset:
            print(
                "[Local prompt history cleared.]",
                flush=True,
            )

        if changed or reset:
            # Recreate the public PromptSession instead of modifying
            # prompt_toolkit's private history-loading internals.
            self._rebuild()

        self.invalidate()

    def prompt_ready(self, identifier):
        if not isinstance(identifier, int) or identifier < 1:
            raise RuntimeError("Invalid backend prompt identifier.")

        self.operation = identifier
        self.phase = "prompt"
        self.interrupt_requested = None
        self.typing_stopped_speech = False

        # Old operation IDs are no longer relevant to incoming answers.
        self.cancelled_operations = {
            value
            for value in self.cancelled_operations
            if value >= identifier
        }

        self.invalidate()

    def working(self, identifier):
        if identifier == self.operation:
            if self.interrupt_requested == identifier:
                self.phase = "interrupting"
            else:
                self.phase = "working"
            self.invalidate()

    def request_interrupt(self):
        if self.stopping or self.phase == "closing":
            return

        self.speech.interrupt()

        if self.interrupt_requested == self.operation:
            return

        self.interrupt_requested = self.operation
        self.cancelled_operations.add(self.operation)
        self.phase = "interrupting"

        message = {
            "op": "interrupt",
            "id": self.operation,
        }

        if self.session is not None:
            self.session.default_buffer.reset()

            pending = getattr(self, "_pending_controls", None)

            if pending is None:
                pending = []
                self._pending_controls = pending

            # Send after the prompt application has restored its display.
            # Preserve ordering if Enter and Ctrl+C arrive close together.
            pending.append(message)
            self._rebuild()
        else:
            self.outgoing.put_nowait(message)

        self.invalidate()

    def request_disconnect(self):
        self.speech.interrupt()
        self.phase = "closing"
        self.force_close.set()
        self.stop()

    def stop(self):
        self.stopping = True
        self.available.set()
        self._rebuild()

    def _send_text(self, text, buffer=None):
        if self.phase != "prompt":
            return

        if not text.strip():
            if buffer is not None:
                buffer.reset()
            return

        message = {
            "op": "input",
            "id": self.operation,
            "text": text,
        }

        encoded = (
            json.dumps(message, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        if len(encoded) > MAX_INPUT_BYTES:
            print(
                "\n[Request exceeds the client's 8 MiB input limit.]",
                file=sys.stderr,
                flush=True,
            )
            return

        self.speech.interrupt()

        if buffer is not None:
            buffer.append_to_history()

        self.phase = "submitted"

        if self.session is not None:
            pending = getattr(self, "_pending_controls", None)

            if pending is None:
                pending = []
                self._pending_controls = pending

            pending.append(message)

            # Keep the entered text in the buffer. The normal final prompt
            # render leaves one transcript copy on screen.
            self._rebuild()
        else:
            self.outgoing.put_nowait(message)

        self.invalidate()

    def _message(self):
        if self.phase == "prompt":
            return "\n===\nHuman Input: "
        return ""

    def _toolbar(self):
        if not self.settings["toolbar"]:
            return ""

        if self.phase == "prompt":
            return (
                " Enter: send | Ctrl+O: newline | Up/Down: history | "
                "Ctrl+R: search | Ctrl+C: remove/exit | Ctrl+Q: disconnect "
            )

        if self.phase == "interrupting":
            return (
                " Interrupt requested; waiting for Hugh | "
                "Ctrl+Q: disconnect "
            )

        if self.phase == "startup":
            return (
                " Hugh is starting | Ctrl+C: cancel startup | "
                "Ctrl+Q: disconnect "
            )

        if self.phase == "closing":
            return " Closing Hugh... "

        return (
            " Hugh is working | Ctrl+C: interrupt and stop speech | "
            "Ctrl+Q: disconnect "
        )

    def _bindings(self):
        bindings = KeyBindings()
        editing = Condition(lambda: self.phase == "prompt")

        @bindings.add("enter")
        def submit(event):
            self._send_text(
                event.current_buffer.text,
                event.current_buffer,
            )

        @bindings.add("c-c")
        def interrupt(event):
            self.request_interrupt()

        @bindings.add("c-q")
        def disconnect(event):
            self.request_disconnect()

        @bindings.add("c-d")
        def eof_or_delete(event):
            if self.phase != "prompt":
                return

            buffer = event.current_buffer

            if not buffer.text:
                self._send_text("/quit")
            else:
                buffer.delete()

        @bindings.add("c-o", filter=editing)
        def newline(event):
            if self.settings["multiline"]:
                event.current_buffer.insert_text("\n")

        @bindings.add("up", filter=editing)
        def previous(event):
            event.current_buffer.history_backward()

        @bindings.add("down", filter=editing)
        def following(event):
            event.current_buffer.history_forward()

        @bindings.add("escape", "up", filter=editing)
        def upward(event):
            event.current_buffer.cursor_up()

        @bindings.add("escape", "down", filter=editing)
        def downward(event):
            event.current_buffer.cursor_down()

        return bindings

    async def run(self):
        self._state_changed = asyncio.Event()

        if not hasattr(self, "_pending_controls"):
            self._pending_controls = []

        await self.available.wait()

        if self.stopping:
            return

        output = create_output()

        with contextlib.closing(create_input()) as input_device:
            with input_device.raw_mode():

                # Keyboard input remains raw during work so a typed Ctrl+C
                # becomes a protocol command rather than killing SSH.
                # Keep ordinary terminal newline processing for direct
                # stdout/stderr output outside the prompt application.
                try:
                    import termios

                    descriptor = input_device.fileno()
                    attributes = termios.tcgetattr(descriptor)
                    attributes[1] |= termios.OPOST | termios.ONLCR
                    termios.tcsetattr(
                        descriptor, termios.TCSANOW, attributes
                    )
                except (
                    ImportError,
                    AttributeError,
                    OSError,
                    ValueError,
                    NotImplementedError,
                ):
                    pass

                def busy_keys():
                    try:
                        presses = input_device.read_keys()
                    except (EOFError, OSError):
                        self.request_disconnect()
                        return

                    for press in presses:
                        key = getattr(press.key, "value", press.key)

                        if key == "c-c":
                            self.request_interrupt()
                        elif key == "c-q":
                            self.request_disconnect()
                        elif getattr(press, "data", ""):
                            # Ordinary typing during work can silence audio,
                            # but is not silently saved as the next request.
                            self.speech.interrupt()

                    if input_device.closed:
                        self.request_disconnect()

                while not self.stopping:
                    if self.phase != "prompt":
                        self._state_changed.clear()

                        if self.stopping or self.phase == "prompt":
                            continue

                        # No PromptSession application is running here.
                        # Hugh's text goes directly to the terminal.
                        with input_device.attach(busy_keys):
                            await self._state_changed.wait()

                        continue

                    session = PromptSession(
                        input=input_device,
                        output=output,
                        history=self.history,
                        key_bindings=self._bindings(),
                        multiline=Condition(
                            lambda: self.settings["multiline"]
                        ),
                        enable_history_search=True,
                        wrap_lines=True,
                        mouse_support=False,
                    )

                    session.default_buffer.read_only = Condition(
                        lambda: self.phase != "prompt"
                    )
                    self.session = session

                    def text_changed(buffer):
                        if (
                            self.phase == "prompt"
                            and buffer.text
                            and not self.typing_stopped_speech
                        ):
                            self.typing_stopped_speech = True
                            self.speech.interrupt()

                    session.default_buffer.on_text_changed += text_changed

                    try:
                        if self.phase == "prompt" and not self.stopping:
                            # Protect the editor only while it is actually
                            # accepting input. Do not wrap streamed work.
                            with patch_stdout(raw=True):
                                await session.prompt_async(
                                    "\n===\nHuman Input: ",
                                    bottom_toolbar=self._toolbar,
                                )
                    except EOFError:
                        if self.phase == "prompt":
                            self._send_text("/quit")
                    finally:
                        self.session = None

                    pending = self._pending_controls
                    self._pending_controls = []

                    if not self.stopping:
                        for message in pending:
                            self.outgoing.put_nowait(message)


# =====================================================================
# Backend output handling
# =====================================================================

class BackendOutput:
    def __init__(self, speech: Speech, editor: Editor):
        self.speech = speech
        self.editor = editor
        self.handshake = False
        self.response = None
        self.exit_status = None

    @staticmethod
    def display(text):
        if text:
            print(text, end="", flush=True)

    def handle(self, event):
        kind = event["event"]

        if kind == "hello":
            if event.get("version") != 1:
                raise RuntimeError("Unsupported backend adapter version.")
            self.handshake = True

        elif kind == "settings":
            self.editor.configure(event)

        elif kind == "text":
            self.display(str(event.get("text", "")))

        elif kind == "prompt":
            self.editor.prompt_ready(event.get("id"))

        elif kind == "working":
            self.editor.working(event.get("id"))

        elif kind == "answer_begin":
            self.response = event.get("response")
            operation = event.get("operation")

            self.speech.begin_response(
                enabled=(
                    operation
                    not in self.editor.cancelled_operations
                )
            )

        elif kind == "answer_delta":
            text = str(event.get("text", ""))
            self.display(text)

            if event.get("response") == self.response:
                self.speech.feed(text)

        elif kind == "answer_end":
            if event.get("response") == self.response:
                self.speech.end_response()
                self.response = None

        elif kind == "answer_abort":
            if event.get("response") == self.response:
                self.speech.interrupt()
                self.response = None

        elif kind == "clear":
            self.speech.interrupt()
            self.display("\033[2J\033[H")

        elif kind == "redraw":
            self.speech.interrupt()
            self.response = None
            self.display("\033[2J\033[H")
            self.display(str(event.get("header", "")) + "\n")
            self.display("Active conversation:\n\n")

            messages = event.get("messages", [])

            if not isinstance(messages, list):
                raise RuntimeError("Invalid conversation redraw.")

            for message in messages:
                if not isinstance(message, dict):
                    raise RuntimeError("Invalid redraw message.")

                label = (
                    "Human Input"
                    if message.get("role") == "user"
                    else "Hugh"
                )
                self.display(
                    "===\n" + label + ": "
                    + str(message.get("text", "")) + "\n\n"
                )

            if not messages:
                self.display("[No conversation messages.]\n\n")

            notice = str(event.get("notice", ""))

            if notice:
                self.display("[" + notice + "]\n\n")

        elif kind == "exit":
            self.exit_status = event.get("status")
            self.editor.phase = "closing"
            self.editor.invalidate()

        else:
            raise RuntimeError(
                "Unknown backend event: " + repr(kind)
            )


# =====================================================================
# Process lifecycle
# =====================================================================

async def stop_transport(process, mode: str):
    if process is None:
        return

    if process.stdin is not None:
        process.stdin.close()

        with contextlib.suppress(
            BrokenPipeError,
            ConnectionResetError,
            OSError,
        ):
            await process.stdin.wait_closed()

    if process.returncode is not None:
        return

    # Closing stdin lets the bridge interrupt active work and allow
    # Hugh's existing handlers to clean up isolated workers and logging.
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass

    def send(sig):
        with contextlib.suppress(ProcessLookupError):
            if mode == "local":
                os.killpg(process.pid, sig)
            else:
                # Never signal our own terminal process group.
                process.send_signal(sig)

    send(signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        send(signal.SIGKILL)
        await process.wait()


async def run_client(mode: str, espeak: str) -> int:
    speech = Speech(espeak)
    outgoing = asyncio.Queue()
    editor = Editor(speech, outgoing)
    backend = BackendOutput(speech, editor)

    transport = None
    tasks = []
    ui_task = None
    writer_task = None
    force_task = None

    loop = asyncio.get_running_loop()
    installed_signals = []

    if mode == "local":
        print("[Hugh Speak: launching Hugh on this computer.]")
    else:
        print(
            "[Hugh Speak: launching Hugh through SSH to "
            + SSH_HOST.strip() + ".]"
        )

    print(
        "[Voice plays locally. Type at the prompt to stop speech.]\n"
        "[Ctrl+C: Hugh's interrupt/remove/exit behavior. "
        "Ctrl+Q: disconnect.]",
        flush=True,
    )

    speech.start()

    with contextlib.nullcontext():
        try:
            launch_environment = os.environ.copy()

            if mode == "ssh":
                launch_environment["SSH_ASKPASS_REQUIRE"] = "never"

            transport = await asyncio.create_subprocess_exec(
                *build_launch_command(mode),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=launch_environment,
                limit=MAX_FRAME_BYTES,
                # SSH needs the controlling terminal for normal
                # authentication. After authentication, the local editor
                # owns raw keyboard input throughout backend work.
                start_new_session=(mode == "local"),
            )

            async def read_output():
                try:
                    while True:
                        raw = await transport.stdout.readline()

                        if not raw:
                            return

                        if len(raw) > MAX_FRAME_BYTES:
                            raise RuntimeError(
                                "Backend output frame exceeds its limit."
                            )

                        line = raw.decode("utf-8", errors="replace")
                        prefix, event = decode_wire_line(line)

                        if prefix:
                            backend.display(prefix)

                        if event is not None:
                            backend.handle(event)
                finally:
                    editor.stop()

            async def read_errors():
                async for text in decoded_chunks(transport.stderr):
                    print(
                        text,
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )

            async def write_controls():
                while True:
                    message = await outgoing.get()
                    encoded = (
                        json.dumps(
                            message,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ) + "\n"
                    ).encode("utf-8")

                    if len(encoded) > MAX_INPUT_BYTES:
                        raise RuntimeError(
                            "Client control/input frame is too large."
                        )

                    try:
                        transport.stdin.write(encoded)
                        await transport.stdin.drain()
                    except (
                        BrokenPipeError,
                        ConnectionResetError,
                    ) as exc:
                        raise RuntimeError(
                            "The connection closed while sending input."
                        ) from exc

            output_task = asyncio.create_task(
                read_output(), name="backend-output"
            )
            error_task = asyncio.create_task(
                read_errors(), name="backend-errors"
            )
            writer_task = asyncio.create_task(
                write_controls(), name="backend-controls"
            )
            ui_task = asyncio.create_task(
                editor.run(), name="local-editor"
            )
            force_task = asyncio.create_task(
                editor.force_close.wait(), name="disconnect-request"
            )

            tasks = [
                output_task,
                error_task,
                writer_task,
                ui_task,
                force_task,
            ]

            for sig, callback in (
                (signal.SIGINT, editor.request_interrupt),
                (signal.SIGTERM, editor.request_disconnect),
            ):
                previous = signal.getsignal(sig)

                try:
                    loop.add_signal_handler(sig, callback)
                except (NotImplementedError, RuntimeError):
                    continue

                installed_signals.append((sig, previous))

            monitored = {
                output_task,
                error_task,
                writer_task,
                ui_task,
                force_task,
                speech.task,
            }

            while True:
                done, _ = await asyncio.wait(
                    monitored,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Retrieve failures before treating EOF as normal.
                for task in done:
                    if task is not force_task:
                        task.result()

                if speech.task in done:
                    raise RuntimeError(
                        "The speech worker stopped unexpectedly."
                    )

                if force_task in done:
                    print(
                        "\n[Disconnect requested; attempting backend cleanup.]",
                        flush=True,
                    )
                    return 130

                if output_task in done:
                    break

                if ui_task in done:
                    if editor.force_close.is_set():
                        return 130

                    if output_task.done():
                        output_task.result()
                        break

                    raise RuntimeError(
                        "The local editor stopped before the backend."
                    )

                # stderr may close before stdout during a normal exit.
                monitored.difference_update(done)

            return_code = await asyncio.wait_for(
                transport.wait(), timeout=10
            )

            await asyncio.wait_for(
                asyncio.shield(error_task), timeout=2
            )

            if return_code == 0 and not backend.handshake:
                raise RuntimeError(
                    "The backend exited without its adapter handshake."
                )

            return return_code if return_code >= 0 else 1

        finally:
            editor.stop()
            speech.interrupt()

            if writer_task is not None and not writer_task.done():
                writer_task.cancel()

            if ui_task is not None and not ui_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(ui_task), timeout=2
                    )
                except (asyncio.TimeoutError, Exception):
                    ui_task.cancel()

            # Keep output readers alive while stdin closure requests
            # graceful backend cleanup.
            try:
                await stop_transport(transport, mode)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()

                if tasks:
                    await asyncio.gather(
                        *tasks, return_exceptions=True
                    )

                await speech.close()

                for sig, previous in installed_signals:
                    loop.remove_signal_handler(sig)

                    with contextlib.suppress(
                        OSError, ValueError, RuntimeError
                    ):
                        signal.signal(sig, previous)


# =====================================================================
# Model-free regression tests
# =====================================================================

def run_self_tests() -> int:
    import tempfile
    import unittest

    class SpeechTests(unittest.IsolatedAsyncioTestCase):
        async def test_editor_uses_supported_prompt_toolkit_apis(self):
            from unittest import mock
            from prompt_toolkit.input import DummyInput
            from prompt_toolkit.output import DummyOutput

            speech = Speech("not-launched")
            editor = Editor(speech, asyncio.Queue())
            editor.settings["toolbar"] = False
            editor.available.set()

            real_prompt_session = PromptSession
            constructed = []
            busy_attachments = []

            class TestInput(DummyInput):
                def attach(self, callback):
                    @contextlib.contextmanager
                    def attached():
                        # Startup/work must not have a live prompt editor.
                        self_test.assertIsNone(editor.session)
                        self_test.assertEqual(constructed, [])
                        busy_attachments.append(True)

                        asyncio.get_running_loop().call_soon(
                            editor.prompt_ready, 1
                        )
                        yield

                    return attached()

            self_test = self

            def make_session(*args, **kwargs):
                session = real_prompt_session(*args, **kwargs)
                constructed.append(session)

                async def fake_prompt_async(*args, **kwargs):
                    self.assertFalse(
                        session.default_buffer.read_only()
                    )

                    editor.working(1)
                    self.assertTrue(
                        session.default_buffer.read_only()
                    )

                    editor.stop()

                session.prompt_async = fake_prompt_async
                return session

            this_module = sys.modules[__name__]

            try:
                with (
                    mock.patch.object(
                        this_module,
                        "create_input",
                        side_effect=TestInput,
                    ),
                    mock.patch.object(
                        this_module,
                        "create_output",
                        side_effect=DummyOutput,
                    ),
                    mock.patch.object(
                        this_module,
                        "PromptSession",
                        side_effect=make_session,
                    ),
                ):
                    await asyncio.wait_for(editor.run(), timeout=3)

                self.assertEqual(len(busy_attachments), 1)
                self.assertEqual(len(constructed), 1)
                self.assertTrue(editor.stopping)
            finally:
                editor.stop()
                await speech.close()

        async def test_short_answer_flushes_at_end(self):
            speech = Speech("not-launched")

            try:
                speech.begin_response()
                speech.feed("A short sentence. ")
                self.assertTrue(speech.queue.empty())

                speech.end_response()
                _, text = speech.queue.get_nowait()

                self.assertEqual(text, "A short sentence.")
            finally:
                await speech.close()

        async def test_partial_text_waits_for_a_smoother_chunk(self):
            speech = Speech("not-launched")

            try:
                speech.begin_response()
                speech.feed("A useful beginning without punctuation ")
                self.assertTrue(speech.queue.empty())
                self.assertIsNone(speech.timer)

                speech.feed(
                    "Additional words complete this sentence and provide "
                    "enough text for a smooth, continuous spoken segment. "
                )

                _, text = speech.queue.get_nowait()
                self.assertGreaterEqual(len(text), 100)
                self.assertTrue(text.endswith("."))
            finally:
                await speech.close()

        async def test_interrupted_response_cannot_resume(self):
            speech = Speech("not-launched")
            speech.begin_response()
            speech.feed("Old answer. ")
            speech.interrupt()
            speech.feed("Late old answer. ")
            speech.end_response()

            self.assertTrue(speech.queue.empty())

            speech.begin_response()
            speech.feed("New answer. ")

            _, text = speech.queue.get_nowait()
            self.assertEqual(text, "New answer.")
            await speech.close()

        async def test_chunking_preserves_words_and_text_order(self):
            speech = Speech("not-launched")

            try:
                speech.begin_response()

                original = (
                    "These words form a continuous spoken response "
                    "with ordinary spacing. "
                ) * 10 + "An unfinished wor"

                speech.feed(original)
                speech.feed("d.")
                speech.end_response()

                chunks = []

                while not speech.queue.empty():
                    _, text = speech.queue.get_nowait()
                    chunks.append(text)

                self.assertEqual(
                    " ".join(chunks),
                    clean_speech_text(original + "d."),
                )
            finally:
                await speech.close()

    class ProtocolTests(unittest.TestCase):
        def test_bridge_source_compiles(self):
            compile(BACKEND_BRIDGE, "<hugh-speak-bridge>", "exec")

        def test_literal_prompt_markers_remain_text(self):
            original = (
                "\n===\nHuman Input: literal example\n"
                + WIRE_PREFIX + " is also literal data."
            )
            line = WIRE_PREFIX + json.dumps({
                "event": "answer_delta",
                "response": 1,
                "operation": 2,
                "text": original,
            }) + "\n"

            prefix, event = decode_wire_line(line)
            self.assertEqual(prefix, "")
            self.assertEqual(event["event"], "answer_delta")
            self.assertEqual(event["text"], original)

        def test_shell_startup_text_is_not_an_answer(self):
            prefix, event = decode_wire_line("Startup notice.\n")
            self.assertEqual(prefix, "Startup notice.\n")
            self.assertIsNone(event)

    class BridgeTests(unittest.IsolatedAsyncioTestCase):
        async def test_multiline_removal_and_busy_interrupt(self):
            fake_source = r'''
import sys
import time
from types import SimpleNamespace

HEADER = "FAKE HUGH"

class PromptEditor:
    def __init__(self, session=None, persistent=False):
        self.session = session
        self.persistent = persistent

class GraniteEngine:
    def generate(self, text, stream=False):
        sys.stdout.write(text)
        sys.stdout.flush()
        return SimpleNamespace(answer=text)

def create_editor(config):
    return PromptEditor()

def read_input(editor, config):
    return input("unused: ")

def redraw(messages, notice=""):
    pass

def clear_screen():
    pass

def parse_request(raw, config):
    return raw

def chat_main(config):
    editor = create_editor(config)
    messages = []
    engine = GraniteEngine()

    while True:
        try:
            raw = read_input(editor, config)
        except EOFError:
            return
        except KeyboardInterrupt:
            if not messages:
                return
            messages.pop()
            redraw(messages, "Removed.")
            continue

        if raw == "/quit":
            return

        try:
            parse_request(raw, config)
            messages.append({
                "role": "user",
                "display_content": raw,
            })

            if raw == "slow":
                print("[slow work active]", flush=True)
                while True:
                    time.sleep(0.05)

            print("\n===\nHugh: ", end="", flush=True)
            engine.generate(raw, stream=True)
            print()
            messages.append({
                "role": "assistant",
                "display_content": raw,
            })
        except KeyboardInterrupt:
            redraw(messages, "Interrupted.")

def main():
    config = SimpleNamespace(
        PROMPT_HISTORY_ENABLED=False,
        PROMPT_MULTILINE=True,
        PROMPT_BOTTOM_TOOLBAR=True,
    )
    chat_main(config)
    return 0
'''
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fake_hugh.py"
                path.write_text(fake_source, encoding="utf-8")

                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-c",
                    BACKEND_BRIDGE,
                    str(path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_FRAME_BYTES,
                )

                async def next_event():
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=5
                    )
                    self.assertTrue(line, "Unexpected backend EOF.")
                    _, event = decode_wire_line(line.decode("utf-8"))
                    self.assertIsNotNone(event)
                    return event

                async def until(kind):
                    collected = []

                    while True:
                        event = await next_event()
                        collected.append(event)

                        if event["event"] == kind:
                            return event, collected

                async def send(op, identifier, text=None):
                    message = {"op": op, "id": identifier}

                    if text is not None:
                        message["text"] = text

                    process.stdin.write(
                        (json.dumps(message) + "\n").encode("utf-8")
                    )
                    await process.stdin.drain()

                try:
                    prompt, _ = await until("prompt")
                    multiline = "first line\n    indented second line"

                    await send("input", prompt["id"], multiline)
                    prompt, events = await until("prompt")

                    spoken = "".join(
                        event.get("text", "")
                        for event in events
                        if event["event"] == "answer_delta"
                    )
                    self.assertEqual(spoken, multiline)

                    await send("interrupt", prompt["id"])
                    redraw_event, _ = await until("redraw")
                    self.assertEqual(
                        [m["role"] for m in redraw_event["messages"]],
                        ["user"],
                    )

                    prompt, _ = await until("prompt")
                    await send("interrupt", prompt["id"])
                    redraw_event, _ = await until("redraw")
                    self.assertEqual(redraw_event["messages"], [])

                    prompt, _ = await until("prompt")
                    await send("input", prompt["id"], "slow")

                    while True:
                        event = await next_event()

                        if (
                            event["event"] == "text"
                            and "[slow work active]"
                            in event.get("text", "")
                        ):
                            break

                    await send("interrupt", prompt["id"])
                    redraw_event, _ = await until("redraw")

                    self.assertEqual(
                        [m["role"] for m in redraw_event["messages"]],
                        ["user"],
                    )

                    prompt, _ = await until("prompt")
                    await send("input", prompt["id"], "/quit")

                    await until("exit")
                    code = await asyncio.wait_for(
                        process.wait(), timeout=5
                    )
                    self.assertEqual(code, 0)

                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()

                    if process.stdin is not None:
                        process.stdin.close()

    suite = unittest.TestSuite()
    loader = unittest.defaultTestLoader

    for test_class in (SpeechTests, ProtocolTests, BridgeTests):
        suite.addTests(loader.loadTestsFromTestCase(test_class))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


# =====================================================================
# Entry point
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Hugh terminal client with local speech and native "
            "conversation-control semantics."
        )
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help=(
            "Run model-free tests without SSH, Granite, or audio."
        ),
    )
    arguments = parser.parse_args()

    if arguments.self_test:
        return run_self_tests()

    mode = CONNECTION_MODE.strip().lower()

    if mode not in {"local", "ssh"}:
        print(
            'Set CONNECTION_MODE to "local" or "ssh".',
            file=sys.stderr,
        )
        return 1

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "Run hugh_speak.py in an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    if (
        not isinstance(WORDS_PER_MINUTE, int)
        or isinstance(WORDS_PER_MINUTE, bool)
        or not 80 <= WORDS_PER_MINUTE <= 450
    ):
        print(
            "WORDS_PER_MINUTE must be an integer from 80 to 450.",
            file=sys.stderr,
        )
        return 1

    if (
        not isinstance(PITCH, int)
        or isinstance(PITCH, bool)
        or not 0 <= PITCH <= 99
    ):
        print(
            "PITCH must be an integer from 0 to 99.",
            file=sys.stderr,
        )
        return 1

    if not isinstance(VOICE, str) or not VOICE.strip():
        print("VOICE must be a nonempty string.", file=sys.stderr)
        return 1

    if (
        SPEECH_START_DELAY <= 0
        or SPEECH_CONTINUE_DELAY <= 0
        or FIRST_CHUNK_MAX_CHARS < 24
        or LATER_CHUNK_MAX_CHARS < 80
    ):
        print(
            "Invalid speech collection settings.",
            file=sys.stderr,
        )
        return 1

    if mode == "ssh" and (
        not SSH_HOST.strip() or SSH_HOST.strip().startswith("-")
    ):
        print("Set SSH_HOST to your SSH destination.", file=sys.stderr)
        return 1

    espeak = shutil.which("espeak-ng")

    if espeak is None:
        print(
            "Install the voice engine:\n"
            "  sudo dnf install espeak-ng",
            file=sys.stderr,
        )
        return 1

    required = "ssh" if mode == "ssh" else "bash"

    if shutil.which(required) is None:
        print(
            "Required program not found: " + required,
            file=sys.stderr,
        )
        return 1

    try:
        return asyncio.run(run_client(mode, espeak))
    except KeyboardInterrupt:
        print("\n[Hugh Speak stopped.]")
        return 130
    except (
        OSError,
        RuntimeError,
        ValueError,
        asyncio.TimeoutError,
    ) as exc:
        print(
            "\n[Hugh Speak error: "
            + type(exc).__name__ + ": " + str(exc) + "]",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
