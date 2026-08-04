from jou.cli import parse_args


class TestParseArgs:
    def test_default(self):
        args = parse_args([])
        assert args.shell is None
        assert args.log_file is None
        assert args.command == []

    def test_shell_option(self):
        args = parse_args(["--shell", "/bin/zsh"])
        assert args.shell == "/bin/zsh"

    def test_log_file_option(self):
        args = parse_args(["--log-file", "/tmp/jou.log"])
        assert args.log_file == "/tmp/jou.log"

    def test_command_after_double_dash(self):
        args = parse_args(["--", "ssh", "myhost"])
        assert args.command == ["ssh", "myhost"]

    def test_all_options(self):
        args = parse_args(["--shell", "/bin/zsh", "--log-file", "/tmp/log", "--", "echo", "hi"])
        assert args.shell == "/bin/zsh"
        assert args.log_file == "/tmp/log"
        assert args.command == ["echo", "hi"]
