"""Drive `th-cli run-tests` / `repeat` through a pseudo-terminal and answer its interactive prompts.

th-cli asks four kinds of questions; each is recognised here:
  * option prompts   'Please enter a number for an option above'  -> picked from `rules` by label
  * text prompts     'Enter value: '                              -> from `text_rules` (literals or @tokens)
  * file uploads     'Enter the path to the file to upload ...'   -> Enter (skip)
An unknown prompt is never guessed: the run is stopped and the prompt text is returned as the reason.
"""
import os
import pty
import re
import select
import subprocess
import time
from typing import List, Optional, Tuple

from . import console
from .thcli import ThCli, shell_join, strip

PROMPT_TRIGGER = "Please enter a number for an option above"
FILE_PROMPT = re.compile(r"Enter the path to the file to upload \(or press Enter to skip\):\s*$")
TEXT_PROMPT = re.compile(r"Enter value(?: \[default: [^\]]*\])?: $")  # printed without a trailing newline
BASE38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."


def qr_discriminator(qr: str) -> int:
    """12-bit discriminator inside a Matter QR payload (MT:...). Bit layout per the Matter spec:
    version 3, vendor 16, product 16, flow 2, rendezvous 8, discriminator 12, passcode 27 (LSB first)."""
    body, raw, i = qr[3:], bytearray(), 0
    while i < len(body):
        chunk = body[i:i + 5]
        i += len(chunk)
        value = sum(BASE38.index(c) * 38 ** k for k, c in enumerate(chunk))
        raw += value.to_bytes({5: 3, 4: 2, 2: 1}[len(chunk)], "little")
    return (int.from_bytes(raw, "little") >> (3 + 16 + 16 + 2 + 8)) & 0xFFF


def answer_for(block: str, rules: List[dict]) -> Optional[str]:
    """Option number for the first rule whose `match` hits the prompt and whose `choose` hits an option label."""
    opts = re.findall(r"(?m)^\s*(\d+)\s*[:=\-\)]?\s+(.+?)\s*$", block)
    for rule in rules:
        if re.search(rule["match"], block):
            for num, label in opts:
                if re.search(rule["choose"], label.strip()):
                    return num
    return None


def file_tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = [ln.strip() for ln in f.read().decode(errors="replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


class PromptDriver:
    def __init__(self, th: ThCli, results, dut) -> None:
        self.th, self.results, self.dut = th, results, dut
        self.last_qr: Optional[str] = None

    def resolve_text(self, value: str) -> Optional[str]:
        """Text answers may be literals or tokens: @dut_qr = the QR code the DUT printed at startup,
        @qr_disc = the discriminator (hex) inside that QR code."""
        if value == "@dut_qr":
            try:
                with open(self.dut.log_path or "", errors="replace") as f:
                    m = re.search(r"SetupQRCode: \[(MT:[^\]]+)\]", f.read())
            except OSError:
                m = None
            self.last_qr = m.group(1) if m else None
            return self.last_qr
        if value == "@qr_disc":
            return hex(qr_discriminator(self.last_qr)) if self.last_qr else None
        return value

    def run(self, args: List[str], rules: List[dict], timeout: int, idle: int = 180,
            transcript: Optional[str] = None, text_rules: Optional[List[dict]] = None,
            transcript_notes: Optional[List[str]] = None) -> Tuple[str, int, str]:
        """Returns (output, exit code, reason). A non-empty reason means the run did not end by itself
        (unhandled prompt, timeout, idle); the backend run is then aborted so the next step is not blocked."""
        master, slave = pty.openpty()
        proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        tfile = open(transcript, "w") if transcript else None
        if tfile:  # first line: the command that was run; then notes such as how the DUT was started
            tfile.write(f"$ {shell_join(args)}\n" + "".join(f"# {n}\n" for n in (transcript_notes or [])) + "\n")
            tfile.flush()
        buf, since_answer = "", 0
        start = last = beat = time.time()
        why, clean = "", False
        cli_log, cli_log_size = None, -1
        try:
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                eof = False
                if r:
                    try:
                        raw = os.read(master, 65536)
                    except OSError:  # Linux: EIO once th-cli has exited and closed its end of the pty
                        raw = b""
                    # select() keeps reporting an exited pty as readable, so EOF must end the loop explicitly
                    eof = raw == b""
                    chunk = strip(raw.decode(errors="replace"))
                    if chunk:
                        buf += chunk
                        last = time.time()
                        if tfile:
                            tfile.write(chunk)
                            tfile.flush()
                        if cli_log is None:
                            m = re.search(r"Log output in:?\s*(\S+)", buf)
                            cli_log = m.group(1) if m else None
                        pending = buf[since_answer:]
                        if FILE_PROMPT.search(pending):
                            os.write(master, b"\n")  # skip the upload
                            since_answer = len(buf)
                        elif TEXT_PROMPT.search(pending):
                            ans = next((self.resolve_text(r_["value"]) for r_ in (text_rules or [])
                                        if re.search(r_["match"], pending)), None)
                            if ans is None:
                                why = "UNHANDLED TEXT PROMPT (no text_answers rule matched, or its value could " \
                                      "not be resolved). The prompt was:\n" + "\n".join(pending.splitlines()[-8:])
                                break
                            os.write(master, (ans + "\n").encode())
                            since_answer = len(buf)
                        elif PROMPT_TRIGGER in pending:
                            num = answer_for(pending, rules)
                            if num is None:
                                upto = pending[:pending.index(PROMPT_TRIGGER) + len(PROMPT_TRIGGER)]
                                why = "UNHANDLED PROMPT (no answer rule matched). The prompt was:\n" + \
                                      "\n".join(upto.splitlines()[-12:])
                                break
                            os.write(master, (num + "\n").encode())
                            since_answer = len(buf)
                if cli_log and os.path.exists(cli_log):
                    size = os.path.getsize(cli_log)
                    if size != cli_log_size:
                        cli_log_size, last = size, time.time()
                if eof or (proc.poll() is not None and not r):
                    break
                if time.time() - start > timeout:
                    why = f"timeout after {timeout}s"
                    break
                if time.time() - last > idle:
                    lastlines = "\n".join([ln for ln in buf.splitlines() if ln.strip()][-8:])
                    why = (f"no th-cli output and no log growth for {idle}s (waiting on a prompt the harness does not "
                           f"recognise?). Last output:\n{lastlines}")
                    break
                if time.time() - beat > 30:
                    beat = time.time()
                    tail = (cli_log and file_tail(cli_log)) or next(
                        (ln.strip() for ln in reversed(buf.splitlines()) if ln.strip()), "(no output yet)")
                    print(console.paint(f"      [{self.results.elapsed()}] {self.results.progress} | "
                                        f"step {int(beat - start)}s/{timeout}s | {tail[:100]}", "gray"), flush=True)
            clean = not why
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            os.close(master)
            if tfile:
                tfile.close()
            if not clean:
                # th-cli is gone but the backend keeps executing the test. Abort it so the next step does
                # not fail with "Test Engine is busy".
                self.th.run(["abort-testing"], timeout=60)
        return buf, proc.returncode if proc.returncode is not None else -1, why
