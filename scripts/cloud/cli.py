"""Resolve installed CLIs and translate host file arguments across the WSL bridge."""

import re
import shutil

from . import aws_commands


def executable(name):
    value = (
        (shutil.which("docker.exe") if name == "docker" else None)
        or shutil.which(name)
        or shutil.which(name + ".exe")
    )
    if not value:
        raise RuntimeError(f"The required {name} CLI is unavailable.")
    return value


def cli_path(path, command):
    value = str(path)
    if command.endswith(".exe") and value.startswith("/"):
        match = re.fullmatch(r"/mnt/([a-z])/(.+)", value)
        if not match:
            raise ValueError(
                "The Windows CLI requires a path on a mounted Windows drive."
            )
        return match[1].upper() + ":/" + match[2]
    return value


def run(argv, *, runner=None, **kwargs):
    command = executable(argv[0])
    arguments = list(argv[1:])
    if command.endswith(".exe"):
        flags = (
            {"--config"}
            if argv[0] == "docker"
            else {"--kubeconfig", "-f", "-k"}
            if argv[0] == "kubectl"
            else set()
        )
        if argv[0] == "docker" and "build" in arguments:
            flags |= {"-f", "--file"}
            arguments[-1] = cli_path(arguments[-1], command)
        for index, argument in enumerate(arguments):
            if index and arguments[index - 1] in flags:
                arguments[index] = cli_path(argument, command)
            elif argv[0] == "aws" and argument.startswith("file://"):
                arguments[index] = "file://" + cli_path(argument[7:], command)
    return (runner or aws_commands.run_checked)([command, *arguments], **kwargs)
