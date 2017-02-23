# Copyright 2016-2017 Laszlo Attila Toth
# Distributed under the terms of the GNU General Public License v3

import os
import re
import time
import typing

from dewi.logparser.syslog import Parser
from dewi.module_framework.config import Config
from dewi.module_framework.messages import Messages, Level, CORE_CATEGORY
from dewi.module_framework.module import Module


class LogParserModule:
    def __init__(self,
                 config: Config,
                 messages: Messages,
                 *,
                 add_messages_to_config: bool = False,
                 messages_config_key: typing.Optional[str] = None):
        self._config = config
        self._messages = messages
        self._messages_config_key = messages_config_key or 'messages'

        if add_messages_to_config:
            self.add_message = self._add_message_to_config_too

    def set(self, entry: str, value):
        self._config.set(entry, value)

    def append(self, entry: str, value):
        self._config.append(entry, value)

    def get(self, entry: str):
        return self._config.get(entry)

    def _add_message(self, level: Level, category, message: str,
                     *,
                     hint: typing.Optional[typing.Union[typing.List[str], str]] = None,
                     details: typing.Optional[typing.Union[typing.List[str], str]] = None):
        self._messages.add(level, category, message, hint=hint, details=details)

    def _add_message_to_config_too(self, level: Level, category, message: str,
                                   *,
                                   hint: typing.Optional[typing.Union[typing.List[str], str]] = None,
                                   details: typing.Optional[typing.Union[typing.List[str], str]] = None):
        self._messages.add(level, category, message, hint=hint, details=details)

        msg_dict = dict(
            level=level.name,
            category=category,
            message=message,
        )

        if hint:
            if isinstance(hint, str):
                hint = [hint]
            msg_dict['hint'] = hint

        if details:
            if isinstance(details, str):
                details = [details]
            msg_dict['details'] = details

        self._config.append(
            self._messages_config_key,
            msg_dict
        )

    add_message = _add_message

    def get_registration(self) -> typing.List[typing.Dict[str, typing.Union[str, callable]]]:
        return []

    def start(self):
        pass

    def finish(self):
        pass


class _Pattern:
    def __init__(self, config: typing.Dict[str, typing.Union[str, callable]]):
        self.program = config.get('program', '')
        self.message_substring = config.get('message_substring', '')
        self.callback = config['callback']
        regex = config.get('message_regex', '')

        if regex:
            self.message_regex = re.compile(regex)
            self.process = self.process_regex
        else:
            self.message_regex = ''
            if self.message_substring:
                self.process = self.process_substring
            else:
                self.process = self.callback

    def process_regex(self, time, program, pid, msg):
        m = self.message_regex.match(msg)

        if m:
            self.callback(time, program, pid, msg)

    def process_substring(self, time, program, pid, msg):
        if self.message_substring in msg:
            self.callback(time, program, pid, msg)


class LogHandlerModule(Module):
    """
    @type modules typing.List[LogParserModule]
    """

    def __init__(self, config: Config, messages: Messages, base_path: str):
        """
        base_path: which contains the directory of log messages
        It can be e.g. '/var'
        """
        super().__init__(config, messages)
        self._log_dir = os.path.join(base_path, 'logs')
        if not os.path.exists(self._log_dir):
            self._log_dir = os.path.join(base_path, 'log')
        if not os.path.exists(self._log_dir):
            self._log_dir = os.path.join(base_path, 'var_log')

        self.parser = Parser()
        self.modules = list()
        self._program_parsers = dict()
        self._other_parsers = set()

    def provide(self):
        return 'log'

    def register_module(self, m: type):
        self.modules.append(m(self._config, self._messages))

    def run(self):
        self._init_parsers()
        files = self._collect_files()
        self._process_files(files)
        self._finalize_parsers()

    def _init_parsers(self):
        for module in self.modules:
            registrations = module.get_registration()
            for reg in registrations:
                if 'program' in reg:
                    self._add_to_map(self._program_parsers, reg['program'], _Pattern(reg))
                else:
                    self._other_parsers.add(_Pattern(reg))
            module.start()

    @staticmethod
    def _add_to_map(dictionary, key, value):
        if key not in dictionary:
            dictionary[key] = set()

        dictionary[key].add(value)

    def _finalize_parsers(self):
        for module in self.modules:
            module.finish()

    def _collect_files(self):
        date_file_map = dict()
        files = os.listdir(self._log_dir)
        for file in files:
            if not file.startswith('messages-') and not file.startswith('syslog-'):
                continue

            filename = os.path.join(self._log_dir, file)
            with open(filename, encoding='UTF-8', errors='surrogateescape') as f:
                line = f.readline()
                parsed = self.parser.parse_date(line)
                date_file_map[parsed.group('date')] = filename

        return [date_file_map[k] for k in sorted(date_file_map.keys())]

    def _process_files(self, files: typing.List[str]):
        start = time.clock()
        cnt = 0
        for fn in files:
            with open(fn, encoding='UTF-8', errors='surrogateescape') as f:
                cnt += self._process_file(f)

        end = time.clock()
        diff = end - start
        self.add_message(
            Level.DEBUG, CORE_CATEGORY,
            "Run time: {} line(s) in {} s ({:.2f} kHz)".format(cnt, diff, cnt / diff / 1000))

    def _process_file(self, f):
        # This is a heavily optimized code, please consider the consequences before changing it
        # The loop body must be extremely fast, because it runs on every log line

        cnt = 0
        line = 'non-empty'
        while line:
            line = f.readline()
            cnt += 1
            parts = line.split(' ', 3)
            if len(parts) != 4:
                continue

            if '[' in parts[2]:
                program, pid = parts[2].split('[')
                pid = pid.split(']')[0]
            else:
                program, pid = parts[2][:-1], None

            if program in self._program_parsers:
                for module in self._program_parsers[program]:
                    module.process(parts[0], program, pid, parts[3])
            else:
                for module in self._other_parsers:
                    module.process(parts[0], program, pid, parts[3])

        return cnt