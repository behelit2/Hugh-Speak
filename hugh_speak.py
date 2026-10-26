#!/usr/bin/python3
"""
hugh_speak.py

Run this client from your home directory.

Launches Hugh locally or through SSH and speaks its responses using
the client computer's speakers.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import shlex
import shutil
import signal
import sys


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
#
# Used ONLY when CONNECTION_MODE = "local".
#
# LOCAL_HUGH_DIRECTORY:
#   Directory containing your Hugh program and granite.env.
#   Use "~" if those files are directly in your home directory.
#
# LOCAL_PYTHON:
#   The Python interpreter that already runs Hugh successfully.
#   A relative path is relative to LOCAL_HUGH_DIRECTORY.
# ---------------------------------------------------------------------

LOCAL_HUGH_DIRECTORY = "~/models/granite-4.2-8b"
LOCAL_PYTHON = "~/.venvs/hugh/bin/python3"


# ---------------------------------------------------------------------
# 3. SSH settings
#
# Used ONLY when CONNECTION_MODE = "ssh".
#
# SSH_HOST:
#   Your normal SSH destination, such as "user@192.168.1.50",
#   or a Host alias from ~/.ssh/config.
#
# SSH_HUGH_DIRECTORY:
#   Hugh's directory ON THE SERVER.
#   "~" refers to the server user's home directory.
#
# SSH_PYTHON:
#   Hugh's working Python interpreter ON THE SERVER.
#   A relative path is relative to SSH_HUGH_DIRECTORY.
# ---------------------------------------------------------------------

SSH_HOST = "user@192.168.1.50"
SSH_HUGH_DIRECTORY = "~/models/granite-4.2-8b"
SSH_PYTHON = "~/.venvs/hugh/bin/python3"


# ---------------------------------------------------------------------
# 4. Hugh filenames
#
# Used in BOTH modes.
# These paths are relative to the selected Hugh directory unless absolute.
# ---------------------------------------------------------------------

HUGH_PROGRAM = "hugh_granite.py"
HUGH_ENVIRONMENT = "granite.env"


# ---------------------------------------------------------------------
# 5. Local voice settings
#
# List English voices with:
#   espeak-ng --voices=en
#
# WORDS_PER_MINUTE:
#   Lower = slower. Higher = faster.
# ---------------------------------------------------------------------

VOICE = "en-us-nyc+m7"
PITCH = 38
WORDS_PER_MINUTE = 155


# =====================================================================
# END OF USER SETTINGS
# Normally, nothing below this line needs editing.
# =====================================================================


try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
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


HUGH_MARKER = "\n===\nHugh:"
HUMAN_MARKER = "\n===\nHuman Input:"


def shell_path(value: str) -> str:
    """Quote a path while allowing ~ to refer to Hugh's machine."""
    if value == "~":
        return '"$HOME"'

    if value.startswith("~/"):
        return '"$HOME"/' + shlex.quote(value[2:])

    return shlex.quote(value)


def build_launch_command(mode: str) -> list[str]:
    """Build a local launch or SSH launch using the selected settings."""
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
            # Override only this launched session, after sourcing the file.
            "export SHOW_THINKING=0",
            "export PYTHONUNBUFFERED=1",
            "export PYTHONIOENCODING=utf-8",
            "exec "
            + shell_path(python)
            + " -u "
            + shell_path(HUGH_PROGRAM),
        ]
    )

    if mode == "ssh":
        remote_command = "exec bash -lc " + shlex.quote(command)

        # Normal SSH authentication and host-key checking remain enabled.
        return ["ssh", "-T", SSH_HOST, remote_command]

    return ["bash", "-lc", command]


def clean_speech_text(text: str) -> str:
    """
    Prepare a spoken copy of Hugh's text.

    The original text is still displayed unchanged.
    This is a lightweight Markdown cleaner, not a full Markdown parser.
    """

    # Remove terminal escape sequences.
    text = re.sub(
        r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)",
        "",
        text,
    )
    text = re.sub(
        r"\x1b\[[0-?]*[ -/]*[@-~]",
        "",
        text,
    )
    text = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        " ",
        text,
    )

    # Remove Markdown fence lines, including an optional language name.
    # The contents of code blocks are retained.
    text = re.sub(
        r"(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*$",
        "",
        text,
    )

    # Speak link/image labels rather than Markdown URLs.
    # Example:
    #   [veterinary guidance](https://example.com)
    # becomes:
    #   veterinary guidance
    text = re.sub(
        r"!?\[([^\]]*)\]\([^)]+\)",
        r"\1",
        text,
    )

    # Remove horizontal rules such as ---, ***, and ___.
    text = re.sub(
        r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$",
        "",
        text,
    )

    # Remove bold, italic, and inline-code markers.
    # Do this before removing list prefixes so that a heading such as
    # "**1. Executive Summary**" also loses its list number.
    text = text.replace("*", "").replace("`", "")

    # Remove underscore emphasis while preserving ordinary identifiers
    # such as file_name.
    text = re.sub(r"(?<!\w)_{1,2}(?=\S)", "", text)
    text = re.sub(r"(?<=\S)_{1,2}(?!\w)", "", text)

    # Remove blockquote prefixes.
    text = re.sub(
        r"(?m)^[ \t]*(?:>[ \t]*)+",
        "",
        text,
    )

    # Remove heading prefixes.
    text = re.sub(
        r"(?m)^[ \t]*#{1,6}[ \t]+",
        "",
        text,
    )

    # Remove bullet and numbered-list prefixes.
    # Asterisk bullets have already been removed above.
    text = re.sub(
        r"(?m)^[ \t]*[-+•][ \t]+",
        "",
        text,
    )
    text = re.sub(
        r"(?m)^[ \t]*\d+[.)][ \t]+",
        "",
        text,
    )

    # Convert all remaining whitespace to ordinary spaces.
    return " ".join(text.split())


class Speech:
    """Queue speech without blocking keyboard input or Hugh's output."""

    # Recognize sentence endings even when followed by Markdown emphasis
    # or closing quotation marks. Newlines are also useful boundaries.
    SENTENCE_END = re.compile(
        r"""[.!?]["'”’)*_`]*[ \t]+|\n+"""
    )

    # Combine short headings and short sentences with nearby text.
    MIN_CHUNK_SIZE = 100
    CHUNK_SIZE = 360

    def __init__(self, executable: str):
        self.executable = executable
        self.queue = asyncio.Queue()
        self.generation = 0
        self.pending = ""
        self.process = None
        self.task = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._worker())

    @staticmethod
    def _kill_if_running(process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    @classmethod
    def _terminate(cls, process) -> None:
        if process.returncode is not None:
            return

        with contextlib.suppress(ProcessLookupError):
            process.terminate()

        # Force termination if the speech process does not exit promptly.
        asyncio.get_running_loop().call_later(
            0.3, cls._kill_if_running, process
        )

    def interrupt(self) -> None:
        """Cancel the active speech and discard all queued old speech."""
        self.generation += 1
        self.pending = ""

        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        if self.process is not None:
            self._terminate(self.process)

    def begin_response(self) -> None:
        self.interrupt()

    def _enqueue(self, text: str) -> None:
        text = clean_speech_text(text)

        if text:
            self.queue.put_nowait((self.generation, text))

    def feed(self, text: str) -> None:
        """Collect streamed text into reasonably sized speech chunks."""
        self.pending += text

        while self.pending:
            # Prefer the latest natural boundary within the size limit.
            # Ignore very early boundaries so that headings such as
            # "1. Executive Summary" do not become tiny utterances.
            boundaries = [
                match.end()
                for match in self.SENTENCE_END.finditer(
                    self.pending, 0, self.CHUNK_SIZE
                )
                if match.end() >= self.MIN_CHUNK_SIZE
            ]

            if boundaries:
                cut = boundaries[-1]

            elif len(self.pending) >= self.CHUNK_SIZE:
                # No suitable sentence boundary: split at whitespace.
                cut = max(
                    self.pending.rfind(" ", 0, self.CHUNK_SIZE),
                    self.pending.rfind("\n", 0, self.CHUNK_SIZE),
                    self.pending.rfind("\t", 0, self.CHUNK_SIZE),
                )

                if cut <= 0:
                    cut = self.CHUNK_SIZE

            else:
                # Wait for more text. end_response() flushes any remainder.
                break

            chunk = self.pending[:cut]
            self.pending = self.pending[cut:]
            self._enqueue(chunk)

    def end_response(self) -> None:
        """Finish collecting text, but let queued speech continue."""
        self._enqueue(self.pending)
        self.pending = ""

    async def _worker(self) -> None:
        while True:
            item = await self.queue.get()

            if item is None:
                return

            generation, text = item

            if generation != self.generation:
                continue

            try:
                process = await asyncio.create_subprocess_exec(
                    self.executable,
                    "-v",
                    VOICE,
                    "-p",
                    str(PITCH),
                    "-s",
                    str(WORDS_PER_MINUTE),
                    # Disable eSpeak's final sentence pause because each
                    # queued chunk is a separate process invocation.
                    "-z",
                    "--stdin",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self.process = process

                # Typing may have interrupted speech during process startup.
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
                        + (detail[-400:] or str(process.returncode))
                        + "]",
                        file=sys.stderr,
                        flush=True,
                    )
                    self.interrupt()

            except (OSError, TypeError, ValueError) as exc:
                print(
                    f"\n[Cannot launch speech engine: {exc}]",
                    file=sys.stderr,
                    flush=True,
                )
                self.interrupt()

            finally:
                self.process = None

    async def close(self) -> None:
        self.interrupt()
        self.queue.put_nowait(None)

        if self.task is not None:
            await self.task


class OutputParser:
    """Detect Hugh's headings even when split across incoming reads."""

    def __init__(self, speech: Speech, ready: asyncio.Event):
        self.speech = speech
        self.ready = ready
        self.buffer = ""
        self.in_answer = False
        self.skip_prompt_space = False

    def _emit(self, text: str) -> None:
        if not text:
            return

        print(text, end="", flush=True)

        if self.in_answer:
            self.speech.feed(text)

    def feed(self, text: str) -> None:
        self.buffer += text

        while True:
            if self.skip_prompt_space:
                if not self.buffer:
                    return

                if self.buffer.startswith(" "):
                    self.buffer = self.buffer[1:]

                self.skip_prompt_space = False

            markers = [(HUMAN_MARKER, "human")]

            if not self.in_answer:
                markers.append((HUGH_MARKER, "hugh"))

            matches = []

            for marker, kind in markers:
                position = self.buffer.find(marker)

                if position >= 0:
                    matches.append((position, marker, kind))

            if matches:
                position, marker, kind = min(
                    matches, key=lambda item: item[0]
                )

                self._emit(self.buffer[:position])
                self.buffer = self.buffer[position + len(marker):]

                if kind == "hugh":
                    self.speech.begin_response()
                    self.in_answer = True
                    print(marker, end="", flush=True)

                else:
                    if self.in_answer:
                        self.speech.end_response()

                    self.in_answer = False

                    # Replace the remote prompt with the local input editor.
                    self.skip_prompt_space = True
                    self.ready.set()

                continue

            # Hold possible partial headings so they cannot leak into speech.
            keep = 0

            for marker, _ in markers:
                maximum = min(len(self.buffer), len(marker) - 1)

                for length in range(1, maximum + 1):
                    if self.buffer.endswith(marker[:length]):
                        keep = max(keep, length)

            safe = len(self.buffer) - keep
            self._emit(self.buffer[:safe])
            self.buffer = self.buffer[safe:]
            return

    def finish(self) -> None:
        # An incomplete heading on disconnection is displayed, not spoken.
        if self.buffer:
            print(self.buffer, end="", flush=True)
            self.buffer = ""


async def decoded_chunks(stream):
    """Decode UTF-8 and normalize newlines across read boundaries."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    trailing_cr = ""

    while True:
        data = await stream.read(4096)
        final = not data
        text = trailing_cr + decoder.decode(data, final=final)
        trailing_cr = ""

        if not final and text.endswith("\r"):
            text = text[:-1]
            trailing_cr = "\r"

        text = text.replace("\r\n", "\n").replace("\r", "\n")

        if text:
            yield text

        if final:
            return


async def stop_transport(process, mode: str) -> None:
    """Clean up local Hugh or the SSH client without signaling our own group."""
    if process is None or process.returncode is not None:
        return

    def send_signal(sig):
        with contextlib.suppress(ProcessLookupError):
            if mode == "local":
                # Local Hugh was launched in its own process group.
                os.killpg(process.pid, sig)
            else:
                # SSH shares our terminal session/process group.
                # Signal only the SSH process.
                process.send_signal(sig)

    send_signal(signal.SIGTERM)

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        send_signal(signal.SIGKILL)
        await process.wait()


async def run_client(mode: str, espeak: str) -> int:
    speech = Speech(espeak)
    ready = asyncio.Event()
    output_parser = OutputParser(speech, ready)
    transport = None
    tasks = []

    if mode == "local":
        print("[Hugh Speak: launching Hugh on this computer.]")
    else:
        print(f"[Hugh Speak: launching Hugh through SSH to {SSH_HOST}.]")

    print("[Voice plays locally. Begin typing to stop the previous answer.]")

    with contextlib.nullcontext():
        speech.start()

        try:
            launch_environment = os.environ.copy()

            if mode == "ssh":
                # Passwords and key passphrases must be requested
                # through the terminal, not a graphical askpass program.
                launch_environment["SSH_ASKPASS_REQUIRE"] = "never"

            transport = await asyncio.create_subprocess_exec(
                *build_launch_command(mode),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=launch_environment,

                # Local Hugh gets its own process group.
                # SSH must retain access to our controlling terminal.
                start_new_session=(mode == "local"),
            )

            keys = KeyBindings()

            @keys.add("c-c")
            def quit_at_prompt(event):
                speech.interrupt()
                event.app.exit(exception=EOFError())

            session = PromptSession(
                history=InMemoryHistory(),
                key_bindings=keys,
                multiline=False,
                enable_history_search=True,
            )

            input_active = False
            interrupted = False

            def text_changed(buffer):
                nonlocal interrupted

                if input_active and buffer.text and not interrupted:
                    interrupted = True
                    speech.interrupt()

            session.default_buffer.on_text_changed += text_changed

            async def read_output():
                async for text in decoded_chunks(transport.stdout):
                    output_parser.feed(text)

                output_parser.finish()

            async def read_errors():
                # Diagnostics are displayed, but never spoken.
                async for text in decoded_chunks(transport.stderr):
                    print(
                        text,
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )

            async def read_user():
                nonlocal input_active, interrupted

                while True:
                    await ready.wait()
                    ready.clear()

                    input_active = True
                    interrupted = False

                    try:
                        text = await session.prompt_async(
                            "\n===\nHuman Input: ",
                            bottom_toolbar=(
                                " Enter: send | Up/Down: history | "
                                "Ctrl+C or empty Ctrl+D: quit "
                            ),
                        )
                    except EOFError:
                        text = "/quit"
                    finally:
                        input_active = False

                    # Submission also cancels any remaining old speech.
                    speech.interrupt()

                    # Hugh's plain-input fallback accepts one line per turn.
                    text = text.replace("\r\n", "\n").replace("\r", "\n")

                    if "\n" in text:
                        text = text.replace("\n", " ")
                        print(
                            "[Pasted multiline input joined into one request.]",
                            flush=True,
                        )

                    try:
                        transport.stdin.write(
                            (text + "\n").encode("utf-8")
                        )
                        await transport.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        return

                    if text.strip().lower() == "/quit":
                        return

            output_task = asyncio.create_task(read_output())
            error_task = asyncio.create_task(read_errors())
            input_task = asyncio.create_task(read_user())
            tasks = [output_task, error_task, input_task]

            done, _ = await asyncio.wait(
                {output_task, input_task, speech.task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                task.result()

            if speech.task in done:
                raise RuntimeError("The speech worker stopped unexpectedly.")

            if output_task not in done:
                # Allow Hugh to print its normal closing messages.
                await asyncio.wait_for(output_task, timeout=10)

            return_code = await asyncio.wait_for(
                transport.wait(), timeout=10
            )

            try:
                await asyncio.wait_for(error_task, timeout=2)
            except asyncio.TimeoutError:
                pass

            return return_code if return_code >= 0 else 1

        finally:
            # Cancel the editor first so it restores the terminal.
            for task in tasks:
                if not task.done():
                    task.cancel()

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            await speech.close()
            await stop_transport(transport, mode)


def main() -> int:
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

    if not isinstance(WORDS_PER_MINUTE, int) or not (
        80 <= WORDS_PER_MINUTE <= 450
    ):
        print(
            "WORDS_PER_MINUTE must be an integer between 80 and 450.",
            file=sys.stderr,
        )
        return 1

    if mode == "ssh" and (
        not SSH_HOST.strip() or SSH_HOST.startswith("-")
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
            f"Required program not found: {required}",
            file=sys.stderr,
        )
        return 1

    try:
        return asyncio.run(run_client(mode, espeak))
    except KeyboardInterrupt:
        print("\n[Hugh Speak stopped.]")
        return 130
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        print(f"\n[Hugh Speak error: {exc}]", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
