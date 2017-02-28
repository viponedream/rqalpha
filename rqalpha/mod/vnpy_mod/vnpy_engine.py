# -*- coding: utf-8 -*-
from datetime import datetime
from dateutil.parser import parse
from Queue import Queue
from Queue import Empty
from time import time, sleep
import numpy as np

from rqalpha.model.trade import Trade
from rqalpha.model.order import Order, LimitOrder
from rqalpha.events import EVENT
from rqalpha.utils import get_account_type
from rqalpha.utils.logger import system_log
from rqalpha.const import ACCOUNT_TYPE

from .vn_trader.eventEngine import EventEngine2
from .vn_trader.vtGateway import VtOrderReq, VtCancelOrderReq, VtSubscribeReq
from .vn_trader.eventType import EVENT_CONTRACT, EVENT_ORDER, EVENT_TRADE, EVENT_TICK, EVENT_LOG
from .vn_trader.vtConstant import STATUS_NOTTRADED, STATUS_PARTTRADED, STATUS_ALLTRADED, STATUS_CANCELLED

from .vn_trader.vtConstant import CURRENCY_CNY
from .vn_trader.vtConstant import PRODUCT_FUTURES

from .account_cache import AccountCache
from .utils import SIDE_MAPPING, SIDE_REVERSE, ORDER_TYPE_MAPPING, POSITION_EFFECT_MAPPING, POSITION_EFFECT_REVERSE

_engine = None


def _order_book_id(symbol):
    if len(symbol) < 4:
        return None
    if symbol[-4] not in '0123456789':
        order_book_id = symbol[:2] + '1' + symbol[-3:]
    else:
        order_book_id = symbol
    return order_book_id.upper()


def create_order_from_trade(vnpy_trade):
    return Order.__from_create__(
        calendar_dt=vnpy_trade.tradeTime,
        trading_dt=vnpy_trade.tradeTime,
        order_book_id=_order_book_id(vnpy_trade.symbol),
        quantity=vnpy_trade.volume,
        side=SIDE_REVERSE[vnpy_trade.direction],
        style=LimitOrder(vnpy_trade.price),
        position_effect=POSITION_EFFECT_REVERSE[vnpy_trade.offset]
    )


class RQVNPYEngine(object):
    def __init__(self, env, config):
        self._env = env
        self._config = config
        self.event_engine = EventEngine2()
        self.event_engine.start()

        self.gateway_type = None
        self.vnpy_gateway = None
        self.init_account_time = None

        self._init_gateway()

        self._order_dict = {}
        self._vnpy_order_dict = {}
        self._open_order_dict = {}
        self._trade_dict = {}
        self._contract_dict = {}
        self._account_cache = AccountCache()
        self._tick_que = Queue()

        self._register_event()

    @property
    def open_orders(self):
        return list(self._open_order_dict.values())

    def on_order(self, event):
        vnpy_order = event.dict_['data']
        system_log.debug("on_order {}", vnpy_order.__dict__)
        vnpy_order_id = vnpy_order.vtOrderID

        if self.init_account_time is None:
            self._account_cache.insert_hist_order(vnpy_order)
            return

        try:
            order = self._order_dict[vnpy_order_id]
        except KeyError:
            system_log.error('No Such order in rqalpha query. {}', vnpy_order_id)
            return

        account = self._get_account_for(order)

        order._activate()

        self._env.event_bus.publish_event(EVENT.ORDER_CREATION_PASS, account, order)

        self._vnpy_order_dict[order.order_id] = vnpy_order
        if vnpy_order.status == STATUS_NOTTRADED or vnpy_order.status == STATUS_PARTTRADED:
            self._open_order_dict[vnpy_order_id] = order
        elif vnpy_order.status == STATUS_ALLTRADED:
            if vnpy_order_id in self._open_order_dict:
                del self._open_order_dict[vnpy_order_id]
        elif vnpy_order.status == STATUS_CANCELLED:
            if vnpy_order_id in self._open_order_dict:
                del self._open_order_dict[vnpy_order_id]
            order._mark_rejected('Order was rejected or cancelled by vnpy.')

    def on_trade(self, event):
        vnpy_trade = event.dict_['data']
        system_log.debug("on_trade {}", vnpy_trade.__dict__)

        if self.init_account_time is None:
            self._account_cache.insert_hist_trade(vnpy_trade)

        try:
            order = self._order_dict[vnpy_trade.vtOrderID]
        except KeyError:
            if vnpy_trade.tradeTime > self.init_account_time:
                order = create_order_from_trade(vnpy_trade)
            else:
                return
        account = self._get_account_for(order)
        ct_amount = account.portfolio.positions[order.order_book_id]._cal_close_today_amount(vnpy_trade.volume,
                                                                                             order.side)
        trade = Trade.__from_create__(
            order=order,
            calendar_dt=order.datetime,
            trading_dt=vnpy_trade.tradeTime,
            price=vnpy_trade.price,
            amount=vnpy_trade.volume,
            close_today_amount=ct_amount
        )
        trade._commission = account.commission_decider.get_commission(trade)
        trade._tax = account.tax_decider.get_tax(trade)
        order._fill(trade)
        self._env.event_bus.publish_event(EVENT.TRADE, account, trade)

    def on_contract(self, event):
        contract = event.dict_['data']
        system_log.debug("on_contract {}", contract.__dict__)
        order_book_id = _order_book_id(contract.symbol)
        self._contract_dict[order_book_id] = contract

    def on_tick(self, event):
        vnpy_tick = event.dict_['data']
        system_log.debug("vnpy tick {}", vnpy_tick.__dict__)
        tick = {
            'order_book_id': _order_book_id(vnpy_tick.symbol),
            'datetime': parse('%s %s' % (vnpy_tick.date, vnpy_tick.time)),
            'open': vnpy_tick.openPrice,
            'last': vnpy_tick.lastPrice,
            'low': vnpy_tick.lowPrice,
            'high': vnpy_tick.highPrice,
            'prev_close': vnpy_tick.preClosePrice,
            'volume': vnpy_tick.volume,
            'total_turnover': np.nan,
            'open_interest': vnpy_tick.openInterest,
            'prev_settlement': np.nan,

            'bid': [
                vnpy_tick.bidPrice1,
                vnpy_tick.bidPrice2,
                vnpy_tick.bidPrice3,
                vnpy_tick.bidPrice4,
                vnpy_tick.bidPrice5,
            ],
            'bid_volume': [
                vnpy_tick.bidVolume1,
                vnpy_tick.bidVolume2,
                vnpy_tick.bidVolume3,
                vnpy_tick.bidVolume4,
                vnpy_tick.bidVolume5,
            ],
            'ask': [
                vnpy_tick.askPrice1,
                vnpy_tick.askPrice2,
                vnpy_tick.askPrice3,
                vnpy_tick.askPrice4,
                vnpy_tick.askPrice5,
            ],
            'ask_volume': [
                vnpy_tick.askVolume1,
                vnpy_tick.askVolume2,
                vnpy_tick.askVolume3,
                vnpy_tick.askVolume4,
                vnpy_tick.askVolume5,
            ],

            'limit_up': vnpy_tick.upperLimit,
            'limit_down': vnpy_tick.lowerLimit,
        }
        self._tick_que.put(tick)

    def on_positions(self, event):
        vnpy_position = event.dict_['data']
        system_log.debug("on_positions {}", vnpy_position.__dict__)
        order_book_id = _order_book_id(vnpy_position.symbol)
        self._account_cache.update(order_book_id, vnpy_position)

    def on_account(self, event):
        vnpy_account = event.dict_['data']
        system_log.debug("on_account {}", vnpy_account.__dict__)
        self._account_cache.update_portfolio(vnpy_account)

    def on_log(self, event):
        log = event.dict_['data']
        system_log.debug(log.logContent)

    def on_universe_changed(self, universe):
        for order_book_id in universe:
            self.subscribe(order_book_id)

    def connect(self):
        self.vnpy_gateway.connect(dict(getattr(self._config, self.gateway_type)))
        if self.init_account_time is not None:
            return
        '''
        self.wait_until_connected(timeout=300)
        self.vnpy_gateway.qryAccount()
        self.vnpy_gateway.qryPosition()
        # FIXME: hardcode
        sleep(1)
        account_json = self._account_cache.get_state()
        self._env.broker.init_account(account_json)
        '''
        self._env.broker.init_account(None)
        self.init_account_time = datetime.now()

    def send_order(self, order):
        account = self._get_account_for(order)
        self._env.event_bus.publish_event(EVENT.ORDER_PENDING_NEW, account, order)

        account.append_order(order)

        contract = self._get_contract_from_order_book_id(order.order_book_id)
        if contract is None:
            order._mark_cancelled('No contract exists whose order_book_id is %s' % order.order_book_id)

        if order._is_final():
            return

        order_req = VtOrderReq()
        order_req.symbol = contract.symbol
        order_req.exchange = contract.exchange
        order_req.price = order.price
        order_req.volume = order.quantity
        order_req.direction = SIDE_MAPPING[order.side]
        order_req.priceType = ORDER_TYPE_MAPPING[order.type]
        order_req.offset = POSITION_EFFECT_MAPPING[order.position_effect]
        order_req.currency = CURRENCY_CNY
        order_req.productClass = PRODUCT_FUTURES

        vnpy_order_id = self.vnpy_gateway.sendOrder(order_req)
        self._order_dict[vnpy_order_id] = order

    def cancel_order(self, order):
        account = self._get_account_for(order)
        self._env.event_bus.publish_event(EVENT.ORDER_PENDING_CANCEL, account, order)

        vnpy_order = self._vnpy_order_dict[order.order_id]

        cancel_order_req = VtCancelOrderReq()
        cancel_order_req.symbol = vnpy_order.symbol
        cancel_order_req.exchange = vnpy_order.exchange
        cancel_order_req.sessionID = vnpy_order.sessionID
        cancel_order_req.orderID = vnpy_order.orderID

        self.vnpy_gateway.cancelOrder(cancel_order_req)

    def subscribe(self, order_book_id):
        contract = self._get_contract_from_order_book_id(order_book_id)
        if contract is None:
            return
        subscribe_req = VtSubscribeReq()
        subscribe_req.symbol = contract.symbol
        subscribe_req.exchange = contract.exchange
        subscribe_req.productClass = PRODUCT_FUTURES
        subscribe_req.currency = CURRENCY_CNY
        self.vnpy_gateway.subscribe(subscribe_req)

    def get_tick(self):
        while True:
            try:
                return self._tick_que.get(block=True, timeout=1)
            except Empty:
                system_log.debug("get tick timeout")
                continue

    def wait_until_connected(self, timeout=None):
        start_time = time()
        while True:
            if self.vnpy_gateway.mdConnected and self.vnpy_gateway.tdConnected:
                break
            else:
                if timeout is not None:
                    if time() - start_time > timeout:
                        break

    def exit(self):
        self.vnpy_gateway.close()
        self.event_engine.stop()

    def _init_gateway(self):
        self.gateway_type = self._config.gateway_type
        if self.gateway_type == 'CTP':
            try:
                from .vnpy_gateway import RQVNCTPGateway
                self.vnpy_gateway = RQVNCTPGateway(self.event_engine, self.gateway_type)
                self.vnpy_gateway.setQryEnabled(True)
            except ImportError as e:
                system_log.exception("No Gateway named CTP")
        else:
            system_log.error('No Gateway named {}', self.gateway_type)

    def _register_event(self):
        self.event_engine.register(EVENT_ORDER, self.on_order)
        self.event_engine.register(EVENT_CONTRACT, self.on_contract)
        self.event_engine.register(EVENT_TRADE, self.on_trade)
        self.event_engine.register(EVENT_TICK, self.on_tick)
        self.event_engine.register(EVENT_LOG, self.on_log)

        self._env.event_bus.add_listener(EVENT.POST_UNIVERSE_CHANGED, self.on_universe_changed)

    def _get_contract_from_order_book_id(self, order_book_id):
        try:
            return self._contract_dict[order_book_id]
        except KeyError:
            system_log.error('No such contract whose order_book_id is {} ', order_book_id)

    def _get_account_for(self, order):
        # FIXME: hardcode
        account_type = ACCOUNT_TYPE.FUTURE
        return self._env.broker.get_account()[account_type]
