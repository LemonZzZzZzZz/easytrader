# -*- coding: utf-8 -*-

import pywinauto
import pywinauto.clipboard

from easytrader import grid_strategies
from easytrader.exceptions import TradeError
from easytrader.log import logger
from . import clienttrader


class UniversalClientTrader(clienttrader.BaseLoginClientTrader):
    grid_strategy = grid_strategies.Xls

    @property
    def broker_type(self):
        return "universal"

    def login(self, user, password, exe_path, comm_password=None, **kwargs):
        """
        :param user: 用户名
        :param password: 密码
        :param exe_path: 客户端路径, 类似
        :param comm_password:
        :param kwargs:
        :return:
        """
        try:
            self._app = pywinauto.Application().connect(
                path=self._run_exe_path(exe_path), timeout=1
            )
        # pylint: disable=broad-except
        except Exception:
            self._app = pywinauto.Application().start(exe_path)

            # wait login window ready（带超时上限，防止客户端启动失败时无限等待）
            login_wait_timeout = 30
            login_waited = 0
            while login_waited < login_wait_timeout:
                try:
                    login_window = pywinauto.findwindows.find_window(class_name='#32770', found_index=1)
                    break
                except Exception as e:
                    logger.debug("等待登录窗口第 %d 秒: %s", login_waited + 1, e)
                    self.wait(1)
                    login_waited += 1
            else:
                raise TradeError(
                    "等待同花顺登录窗口超时（{} 秒），请检查客户端是否正常启动".format(
                        login_wait_timeout
                    )
                )

            self.wait(1)
            self._app.window(handle=login_window).Edit1.set_focus()
            self._app.window(handle=login_window).Edit1.type_keys(user)

            self._app.window(handle=login_window).button7.click()

            # detect login is success or not
            # self._app.top_window().wait_not("exists", 100)
            self.wait(5)

            self._app = pywinauto.Application().connect(
                path=self._run_exe_path(exe_path), timeout=10
            )

        self._close_prompt_windows()
        self._main = self._app.window(title="网上股票交易系统5.0")

