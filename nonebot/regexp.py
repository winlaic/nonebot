import asyncio
import re
from datetime import datetime
from functools import partial
from typing import Union, Optional

from aiocqhttp import Event as CQEvent, Message

from nonebot import NoneBot
from nonebot.command import PauseException, FinishException, SwitchException
from nonebot.helpers import context_id, render_expression
from nonebot.log import logger
from nonebot.permission import check_permission
from nonebot.session import BaseSession
from nonebot.typing import *

_tmp_regexp_command = set()


class RegExpCommand:
    def __init__(self, *,
                 func: Callable,
                 permission: int,
                 only_to_me: bool,
                 privileged: bool):
        self._func = func
        self._permission = permission
        self._only_to_me = only_to_me
        self._privileged = privileged

    def __repr__(self):
        return f'<RegExpCommand callback={self._func.__name__} only_to_me={self._only_to_me} privileged={self.privileged}> '

    @property
    def name(self):
        return self._func.__name__

    @property
    def privileged(self):
        return self._privileged

    async def run(self, session, *, check_perm: bool = True, dry: bool = False):
        has_perm = await self._check_perm(session) if check_perm else True
        if self._func and has_perm:
            if dry:
                return True
            await self._func(session)
            return True
        else:
            return False

    async def _check_perm(self, session):
        return await check_permission(session.bot, session.event, self._permission)


class RegExpCommandSession(BaseSession):
    def __init__(self,
                 bot: NoneBot,
                 event: CQEvent,
                 regexp_command: RegExpCommand,
                 initial_arguments: tuple):
        super().__init__(bot, event)
        self.regexp_command = regexp_command
        self._run_future = partial(asyncio.run_coroutine_threadsafe,
                                   loop=bot.loop)
        self._initial_arguments = initial_arguments
        self.followed_text = None
        self.event = event
        self.bot = bot

        # 上一次 running 被设置为 False 的时间。
        self._last_interaction = None  # last interaction time of this session
        self._running = False

    @property
    def initial_arguments(self):
        return self._initial_arguments

    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value) -> None:
        if self._running is True and value is False:
            # change status from running to not running, record the time
            self._last_interaction = datetime.now()
        self._running = value

    @property
    def is_valid(self) -> bool:
        """Check if the session is expired or not."""
        if self.bot.config.SESSION_EXPIRE_TIMEOUT and \
            self._last_interaction and \
            datetime.now() - self._last_interaction > \
            self.bot.config.SESSION_EXPIRE_TIMEOUT:
            return False
        return True

    @property
    def is_first_run(self) -> bool:
        return self._last_interaction is None

    @property
    def params(self):
        return

    def pause(self, message: Optional[Message_T] = None, **kwargs) -> None:
        """Pause the session for further interaction."""
        if message:
            self._run_future(self.send(message, **kwargs))
        raise PauseException

    def finish(self, message: Optional[Message_T] = None, **kwargs) -> None:
        """Finish the session."""
        if message:
            self._run_future(self.send(message, **kwargs))
        raise FinishException

    def switch(self, new_message: Message_T) -> None:
        """
        Finish the session and switch to a new (fake) message event.

        The user may send another command (or another intention as natural
        language) when interacting with the current session. In this case,
        the session may not understand what the user is saying, so it
        should call this method and pass in that message, then NoneBot will
        handle the situation properly.
        """
        if self.is_first_run:
            # if calling this method during first run,
            # we think the command is not handled
            raise FinishException(result=False)

        if not isinstance(new_message, Message):
            new_message = Message(new_message)
        raise SwitchException(new_message)

    pass


_regexp_sessions = {}


class RegExpCommandManager:
    _regexps = dict()
    _switches = dict()

    @classmethod
    def add_regexp_command(cls, exp, regexp_command: RegExpCommand):
        assert isinstance(regexp_command, RegExpCommand)
        if isinstance(exp, str):
            exp = re.compile(exp)
        elif isinstance(exp, re.Pattern):
            pass
        else:
            raise TypeError
        cls._regexps[exp] = regexp_command
        cls.true = True
        cls._switches[regexp_command] = cls.true

    def __init__(self):
        self._regexps = RegExpCommandManager._regexps.copy()

    def parse_regexp_command(self, bot: NoneBot, string: str):
        for exp, regexp_command in self._regexps.items():
            result = exp.match(string)
            if result:
                return regexp_command, result.groups()
        return None, None

    @classmethod
    def remove_regexp_command(cls, regexp_command):
        key_to_remove = []
        for key, value in cls._regexps.items():
            if value is regexp_command:
                key_to_remove.append(key)

        new = {k: v for k, v in cls._regexps.items() if k not in key_to_remove}
        cls._regexps = new


async def handle_regexp(bot: NoneBot, event: CQEvent,
                        manager: RegExpCommandManager) -> Optional[bool]:
    # 尝试从字符中解析出对应的命令。
    regexp_command, initial_arguments = manager.parse_regexp_command(bot, event.message)

    is_privileged_cmd = regexp_command and regexp_command.privileged
    if is_privileged_cmd and regexp_command.only_to_me and not event['to_me']:
        is_privileged_cmd = False

    # 如果这个命令可以在上个会话未结束的时候使用
    # 那么这次使用就关闭交互，不影响 Session。
    disable_interaction = bool(is_privileged_cmd)

    if is_privileged_cmd:
        logger.debug(f'Command {regexp_command.name} is a privileged command')

    # Context 指的是（对话发起人，对话环境）形成的哈希索引。
    ctx_id = context_id(event)

    # 这里防止命令还未执行完毕就开启新命令。
    # 如果不是特殊命令，等待1.5秒。
    if not is_privileged_cmd:
        # wait for 1.5 seconds (at most) if the current session is running
        retry = 5
        while retry > 0 and \
            _regexp_sessions.get(ctx_id) and _regexp_sessions[ctx_id].running:
            retry -= 1
            await asyncio.sleep(0.3)

    check_perm = True
    regexp_session = _regexp_sessions.get(ctx_id) if not is_privileged_cmd else None

    if regexp_session:

        # 这里防止命令还未执行完毕就开启新命令。
        if regexp_session.running:
            logger.warning(f'There is a session of command '
                           f'{regexp_session.regexp_command.name} running, notify the user')
            asyncio.ensure_future(
                bot.send(event,
                         render_expression(bot.config.SESSION_RUNNING_EXPRESSION)))
            # pretend we are successful, so that NLP won't handle it
            return True

        # 不 running 的 Sessoion 是被挂起的。
        # is_valid 检查会话时间是否超过 config.SESSION_EXPIRE_TIMEOUT
        # 如果 Session 还有效，即便是没找到 Command 也直接传入
        if regexp_session.is_valid:
            logger.debug(f'Session of command {regexp_session.regexp_command.name} exists')
            # 因为 Session 还没过期，即使命令中没有AT我也是对我说的。
            event['to_me'] = True
            regexp_session.refresh(event, current_arg=str(event['message']))
            # there is no need to check permission for existing session
            check_perm = False

        # 超过时间就删掉这个 Session
        else:
            # the session is expired, remove it
            logger.debug(f'Session of command {regexp_session.regexp_command.name} is expired')
            if ctx_id in _regexp_sessions:
                del _regexp_sessions[ctx_id]
            regexp_session = None

    if not regexp_session:
        if not regexp_command:
            logger.debug('No registed regular expression matched. Ignored.')
            return False
        if regexp_command.only_to_me and not event['to_me']:
            logger.debug('Not to me, ignored')
            return False
        regexp_session = RegExpCommandSession(bot, event, regexp_command, initial_arguments=initial_arguments)
        logger.debug(f'New session of command {regexp_session.regexp_command.name} created')

    # 检查完 command 有效或者 Session 还在的情况，运行指令。
    return await launch_regexp_command(regexp_session,
                                       ctx_id,
                                       check_perm=check_perm,
                                       disable_interaction=disable_interaction)


# 设置 sessoin 为 Running
# 调用 Command 中的 run()
# 处理命令中发生的异常：挂起、切换、超时等
# 出口：command.run()
async def launch_regexp_command(regexp_session: RegExpCommandSession,
                                ctx_id: str,
                                disable_interaction: bool = False,
                                **kwargs) -> Optional[bool]:
    if not disable_interaction:
        # override session only when interaction is not disabled
        _regexp_sessions[ctx_id] = regexp_session
    try:
        logger.debug(f'Running command {regexp_session.regexp_command.name}')
        regexp_session.running = True
        task = asyncio.create_task(regexp_session.regexp_command.run(regexp_session))
        # future = asyncio.ensure_future(session.cmd.run(session, **kwargs))
        timeout = None
        if regexp_session.bot.config.SESSION_RUN_TIMEOUT:
            timeout = regexp_session.bot.config.SESSION_RUN_TIMEOUT.total_seconds()

        try:
            # 设置超时，运行命令。
            await asyncio.wait_for(task, timeout)
            handled = task.result()
        except asyncio.TimeoutError:
            handled = True
        except (PauseException, FinishException, SwitchException) as e:
            raise e
        except Exception as e:
            logger.error(f'An exception occurred while '
                         f'running command {regexp_session.regexp_command.name}:')
            logger.exception(e)
            handled = True

        # 命令正常执行完成抛出完成异常。
        raise FinishException(handled)

    # 如果命令被暂停，则 running 为 False 但是不删除 Session；
    except PauseException:
        regexp_session.running = False
        if disable_interaction:
            # if the command needs further interaction, we view it as failed
            return False
        logger.debug(f'Further interaction needed for '
                     f'command {regexp_session.regexp_command.name}')
        # return True because this step of the session is successful
        return True
    # 如果命令被完成或者切换，则删除 Session。
    except (FinishException, SwitchException) as e:
        regexp_session.running = False
        logger.debug(f'Session of command {regexp_session.regexp_command.name} finished')
        if not disable_interaction and ctx_id in _regexp_sessions:
            # 如果交互是关闭的，那么 sessions 中就没有这个 session。
            del _regexp_sessions[ctx_id]

        if isinstance(e, FinishException):
            return e.result
        elif isinstance(e, SwitchException):
            # we are guaranteed that the session is not first run here,
            # which means interaction is definitely enabled,
            # so we can safely touch _sessions here.
            if ctx_id in _regexp_sessions:
                # make sure there is no session waiting
                del _regexp_sessions[ctx_id]
            logger.debug(f'Session of command {regexp_session.regexp_command.name} switching, '
                         f'new message: {e.new_message}')
            raise e  # this is intended to be propagated to handle_message()


def on_regexp(exp: Union[str, re.Pattern],
              *,
              only_to_me: bool = True,
              privileged: bool = False,
              permission: int) -> Callable:
    def deco(func: CommandHandler_T) -> CommandHandler_T:
        regexp_cmd = RegExpCommand(func=func, only_to_me=only_to_me, privileged=privileged, permission=permission)
        RegExpCommandManager.add_regexp_command(exp, regexp_cmd)
        _tmp_regexp_command.add(regexp_cmd)
        return func

    return deco
