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

    def test_raw_io_log_file_option(self):
        args = parse_args(["--raw-io-log-file", "/tmp/jou-raw.log"])
        assert args.raw_io_log_file == "/tmp/jou-raw.log"

    def test_log_file_option(self):
        args = parse_args(["--log-file", "/tmp/jou.log"])
        assert args.log_file == "/tmp/jou.log"

    def test_command_after_double_dash(self):
        args = parse_args(["--", "ssh", "myhost"])
        assert args.command == ["ssh", "myhost"]

    def test_line_options_default_to_none(self):
        args = parse_args([])
        assert args.history_lines is None
        assert args.scrollback_lines is None
        assert args.summary_trigger_lines is None
        assert args.summary_max_input_lines is None

    def test_line_options(self):
        args = parse_args([
            "--history-lines", "300",
            "--scrollback-lines", "10000",
            "--summary-trigger-lines", "250",
            "--summary-max-input-lines", "1000",
        ])
        assert args.history_lines == 300
        assert args.scrollback_lines == 10000
        assert args.summary_trigger_lines == 250
        assert args.summary_max_input_lines == 1000

    def test_all_options(self):
        args = parse_args(["--shell", "/bin/zsh", "--raw-io-log-file", "/tmp/raw.log", "--log-file", "/tmp/log", "--", "echo", "hi"])
        assert args.shell == "/bin/zsh"
        assert args.raw_io_log_file == "/tmp/raw.log"
        assert args.log_file == "/tmp/log"
        assert args.command == ["echo", "hi"]
