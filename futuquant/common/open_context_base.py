# -*- coding: utf-8 -*-

import time
from abc import ABCMeta, abstractmethod
from collections import namedtuple
from time import sleep
# from typing import Optional
from threading import Timer
from datetime import datetime
from threading import RLock, Thread
from futuquant.common.utils import *
from futuquant.common.handler_context import HandlerContext
from futuquant.quote.quote_query import InitConnect
from futuquant.quote.quote_response_handler import AsyncHandler_InitConnect
from futuquant.quote.quote_query import GlobalStateQuery
from futuquant.quote.quote_query import KeepAlive, parse_head
from futuquant.common.conn_mng import FutuConnMng
from futuquant.common.network_manager import NetManager
from .err import Err
from .ft_logger import *
from .callback_executor import callback_executor, CallbackItem

_SyncReqRet = namedtuple('_SyncReqRet', ('ret', 'msg'))

class ContextStatus:
    Start = 0
    Connecting = 1
    Ready = 2
    Closed = 3

class OpenContextBase(object):
    """Base class for set context"""
    metaclass__ = ABCMeta

    def __init__(self, host, port, async_enable):
        self.__host = host
        self.__port = port
        self.__async_socket_enable = async_enable
        self._net_mgr = NetManager.default()
        self._handler_ctx = HandlerContext(self._is_proc_run)
        self._lock = RLock()
        self._status = ContextStatus.Start
        self._proc_run = True
        self._sync_req_ret = None
        self._sync_conn_id = 0
        self._conn_id = 0
        self._keep_alive_interval = 10
        self._last_keep_alive_time = datetime.now()
        self._reconnect_timer = None
        self._keep_alive_fail_count = 0
        self._net_mgr.start()
        self._socket_reconnect_and_wait_ready()
        while True:
            with self._lock:
                if self._status == ContextStatus.Ready:
                    break
            sleep(0.02)

    def get_login_user_id(self):
        """
        get login user id
        :return: user id(int64)
        """
        with self._lock:
            return FutuConnMng.get_conn_user_id(self._sync_conn_id)

    def __del__(self):
        self._close()

    @abstractmethod
    def close(self):
        """
        to call close old obj before loop create new, otherwise socket will encounter error 10053 or more!
        """
        self._close()

    @abstractmethod
    def on_api_socket_reconnected(self):
        """
        callback after reconnect ok
        """
        # logger.debug("on_api_socket_reconnected obj ID={}".format(id(self)))
        return RET_OK, ''

    def _close(self):
        with self._lock:
            if self._status == ContextStatus.Closed:
                return
            self._status = ContextStatus.Closed
            net_mgr = self._net_mgr
            conn_id = self._conn_id
            self._conn_id = 0
            self._net_mgr = None
            self.stop()
            self._handlers_ctx = None
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
        if conn_id > 0:
            net_mgr.close(conn_id)
        net_mgr.stop()

    def start(self):
        """
        启动异步接收推送数据
        """
        with self._lock:
            self._proc_run = True

    def stop(self):
        """
        停止异步接收推送数据
        """
        with self._lock:
            self._proc_run = False

    def set_handler(self, handler):
        """
        设置异步回调处理对象

        :param handler: 回调处理对象，必须是以下类的子类实例

                    ===============================    =========================
                     类名                                 说明
                    ===============================    =========================
                    StockQuoteHandlerBase               报价处理基类
                    OrderBookHandlerBase                摆盘处理基类
                    CurKlineHandlerBase                 实时k线处理基类
                    TickerHandlerBase                   逐笔处理基类
                    RTDataHandlerBase                   分时数据处理基类
                    BrokerHandlerBase                   经济队列处理基类
                    ===============================    =========================

        :return: RET_OK: 设置成功

                RET_ERROR: 设置失败
        """
        with self._lock:
            if self._handler_ctx is not None:
                return self._handler_ctx.set_handler(handler)
        return RET_ERROR

    def set_pre_handler(self, handler):
        '''set pre handler'''
        with self._lock:
            if self._handler_ctx is not None:
                return self._handler_ctx.set_pre_handler(handler)
        return RET_ERROR

    def _is_proc_run(self):
        with self._lock:
            return self._proc_run

    def _get_sync_query_processor(self, pack_func, unpack_func, is_create_socket=True):
        """
        synchronize the query processor
        :param pack_func: back
        :param unpack_func: unpack
        :return: sync_query_processor
        """

        def sync_query_processor(**kargs):
            """sync query processor"""
            while True:
                with self._lock:
                    if self._status == ContextStatus.Ready:
                        net_mgr = self._net_mgr
                        conn_id = self._conn_id
                        break
                sleep(0.01)

            try:
                ret_code, msg, req_str = pack_func(**kargs)
                if ret_code != RET_OK:
                    return ret_code, msg, None

                ret_code, msg, rsp_str = net_mgr.sync_query(conn_id, req_str)
                if ret_code != RET_OK:
                    return ret_code, msg, None

                ret_code, msg, content = unpack_func(rsp_str)
                if ret_code != RET_OK:
                    return ret_code, msg, None
            except Exception as e:
                logger.error(traceback.format_exc())
                return RET_ERROR, str(e), None

            return RET_OK, msg, content

        return sync_query_processor

    def _send_async_req(self, req_str):
        conn_id = 0
        net_mgr = None
        with self._lock:
            if self._status != ContextStatus.Ready:
                return RET_ERROR, 'Context closed or not ready'
            conn_id = self._conn_id
            net_mgr = self._net_mgr
        return net_mgr.send(conn_id, req_str)

    def _socket_reconnect_and_wait_ready(self):
        """
        sync_socket & async_socket recreate
        :return: (ret, msg)
        """
        logger.info("Start connecting: host={}; port={};".format(self.__host, self.__port))
        with self._lock:
            self._status = ContextStatus.Connecting
            # logger.info("try connecting: host={}; port={};".format(self.__host, self.__port))
            ret, msg, conn_id = self._net_mgr.connect((self.__host, self.__port), self, 5)
            if ret == RET_OK:
                self._conn_id = conn_id
            else:
                logger.warning(msg)

        if ret == RET_OK:
            while True:
                with self._lock:
                    if self._sync_req_ret is not None:
                        if self._sync_req_ret.ret == RET_OK:
                            self._status = ContextStatus.Ready
                        else:
                            ret, msg = self._sync_req_ret.ret, self._sync_req_ret.msg
                        self._sync_req_ret = None
                        break
                sleep(0.01)

        if ret == RET_OK:
            ret, msg = self.on_api_socket_reconnected()
        else:
            self._wait_reconnect()
        return RET_OK, ''

    def get_sync_conn_id(self):
        with self._lock:
            return self._sync_conn_id

    def get_async_conn_id(self):
        return self.get_sync_conn_id()

    def get_global_state(self):
        """
        获取全局状态

        :return: (ret, data)

                ret == RET_OK data为包含全局状态的字典，含义如下

                ret != RET_OK data为错误描述字符串

                =====================   ===========   ==============================================================
                key                      value类型                        说明
                =====================   ===========   ==============================================================
                market_sz               str            深圳市场状态，参见MarketState
                market_us               str            美国市场状态，参见MarketState
                market_sh               str            上海市场状态，参见MarketState
                market_hk               str            香港市场状态，参见MarketState
                market_future           str            香港期货市场状态，参见MarketState
                server_ver              str            FutuOpenD版本号
                trd_logined             str            '1'：已登录交易服务器，'0': 未登录交易服务器
                qot_logined             str            '1'：已登录行情服务器，'0': 未登录行情服务器
                timestamp               str            Futu后台服务器当前时间戳(秒)
                local_timestamp         double         FutuOpenD运行机器当前时间戳(
                =====================   ===========   ==============================================================
        :example:

        .. code:: python

        from futuquant import *
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        print(quote_ctx.get_global_state())
        quote_ctx.close()
        """
        query_processor = self._get_sync_query_processor(
            GlobalStateQuery.pack_req, GlobalStateQuery.unpack_rsp)

        kargs = {
            'user_id': self.get_login_user_id(),
            'conn_id': self.get_sync_conn_id(),
        }
        ret_code, msg, state_dict = query_processor(**kargs)
        if ret_code != RET_OK:
            return ret_code, msg

        return RET_OK, state_dict

    def on_connected(self, conn_id):
        logger.info('Connected : conn_id={0}; '.format(conn_id))
        kargs = {
            'client_ver': int(SysConfig.get_client_ver()),
            'client_id': str(SysConfig.get_client_id()),
            'recv_notify': True,
        }

        ret, msg, req_str = InitConnect.pack_req(**kargs)
        if ret == RET_OK:
            ret, msg = self._net_mgr.send(conn_id, req_str)
        else:
            logger.warning(make_log_msg('InitConnect.pack_req fail', msg=msg))

        if ret != RET_OK:
            with self._lock:
                self._sync_req_ret = _SyncReqRet(ret, msg)

    def on_error(self, conn_id, err):
        logger.warning('Connect error: conn_id={0}; err={1};'.format(conn_id, err))
        with self._lock:
            if self._status != ContextStatus.Connecting:
                self._wait_reconnect()
            else:
                self._sync_req_ret = _SyncReqRet(RET_ERROR, str(err))

    def on_closed(self, conn_id):
        logger.warning('Connect closed: conn_id={0}'.format(conn_id))
        with self._lock:
            if self._status != ContextStatus.Connecting:
                self._wait_reconnect()
            else:
                self._sync_req_ret = _SyncReqRet(RET_ERROR, 'Connection closed')

    def on_connect_timeout(self, conn_id):
        logger.warning('Connect timeout: conn_id={0}'.format(conn_id))
        with self._lock:
            self._sync_req_ret = _SyncReqRet(RET_ERROR, Err.Timeout.text)

    def on_packet(self, conn_id, proto_info, ret_code, msg, rsp_pb):
        if proto_info.proto_id == ProtoId.InitConnect:
            self._handle_init_connect(conn_id, proto_info.proto_id, ret_code, msg, rsp_pb)
        elif proto_info.proto_id == ProtoId.KeepAlive:
            self._handle_keep_alive(conn_id, proto_info.proto_id, ret_code, msg, rsp_pb)
        elif ret_code == RET_OK:
            item = CallbackItem(self, proto_info.proto_id, rsp_pb)
            callback_executor.queue.put(item)

    def on_activate(self, conn_id, now):
        with self._lock:
            if self._status != ContextStatus.Ready:
                return
            time_elapsed = now - self._last_keep_alive_time
            if time_elapsed.total_seconds() < self._keep_alive_interval:
                return

            logger.debug("Keepalive: conn_id={};".format(conn_id))
            ret, msg, req = KeepAlive.pack_req(self.get_sync_conn_id())
            if ret != RET_OK:
                logger.warning("KeepAlive.pack_req fail: {0}".format(msg))
                return
            ret, msg = self._net_mgr.send(conn_id, req)
            if ret != RET_OK:
                return

            self._last_keep_alive_time = now

    def packet_callback(self, proto_id, rsp_pb):
        with self._lock:
            if self._status != ContextStatus.Ready:
                return

            handler_ctx = self._handler_ctx
        if handler_ctx:
            handler_ctx.recv_func(rsp_pb, proto_id)

    def _handle_init_connect(self, conn_id, proto_info, ret, msg, rsp_pb):
        data = None
        if ret == RET_OK:
            ret, msg, data = InitConnect.unpack_rsp(rsp_pb)

        with self._lock:
            self._sync_req_ret = _SyncReqRet(ret, msg)
            if ret == RET_OK:
                conn_info = copy(data)
                self._sync_conn_id = conn_info['conn_id']
                self._keep_alive_interval = conn_info['keep_alive_interval'] * 4 / 5
                self._net_mgr.set_conn_info(conn_id, conn_info)
                self._last_keep_alive_time = datetime.now()
                FutuConnMng.add_conn(conn_info)
                logger.info(FTLog.make_log_msg("InitConnect ok", conn_id=conn_id, info=conn_info))
            else:
                logger.warning(FTLog.make_log_msg("InitConnect error", msg=msg))
                self._wait_reconnect()

    def _handle_keep_alive(self, conn_id, proto_info, ret_code, msg, rsp_pb):
        should_reconnect = False
        with self._lock:
            if ret_code == RET_OK:
                self._keep_alive_fail_count = 0
            else:
                self._keep_alive_fail_count += 1

            if self._keep_alive_fail_count >= 3:
                logger.warning('Fail to recv KeepAlive for 3 times')
                should_reconnect = True

        if should_reconnect:
            self._wait_reconnect()

    def _wait_reconnect(self):
        wait_reconnect_interval = 8
        net_mgr = None
        conn_id = 0
        with self._lock:
            if self._status == ContextStatus.Closed or self._reconnect_timer is not None:
                return
            logger.info('Wait reconnect in {} seconds: host={}; port={};'.format(wait_reconnect_interval,
                                                                                 self.__host,
                                                                                 self.__port))
            net_mgr = self._net_mgr
            conn_id = self._conn_id
            self._status = ContextStatus.Connecting
            self._sync_conn_id = 0
            self._conn_id = 0
            self._keep_alive_fail_count = 0
            self._reconnect_timer = Timer(wait_reconnect_interval, self._reconnect)
            self._reconnect_timer.start()

        net_mgr.close(conn_id)

    def _reconnect(self):
        with self._lock:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
            if self._status != ContextStatus.Connecting:
                return

        self._socket_reconnect_and_wait_ready()


