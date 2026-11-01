"""用虚拟时间验证账号组件就绪延迟、登出稳定时间与验证优先级。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.errors import Cancelled, VerificationRequired
from jd.auth import AuthState
from jd.login import Login


class LoginWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0.0
        self.login = Login(SimpleNamespace(
            cfg=SimpleNamespace(jd={"login_settle_seconds": .2}),
            scheduler=SimpleNamespace(check_stop=Mock())))

    async def sleep(self, seconds):
        self.now += seconds

    async def settle(self, status, *, stable=False, timeout=1):
        self.login.status = AsyncMock(side_effect=status)
        with patch('jd.login.time', SimpleNamespace(monotonic=lambda: self.now)), \
                patch('jd.login.asyncio', SimpleNamespace(sleep=self.sleep)):
            return await self.login.settled(timeout, require_stable_account=stable)

    async def test_refresh_resumes_within_one_tenth_second_when_account_appears(self):
        def status():
            return (AuthState('authenticated', '账号已出现') if self.now >= .03
                    else AuthState('logged_out', '登录占位入口'))
        result = await self.settle(status)
        self.assertEqual(result.state, 'authenticated')
        self.assertLess(self.now, .1)

    async def test_ready_account_needs_no_extra_wait(self):
        result = await self.settle(lambda: AuthState('authenticated', '账号已出现'))
        self.assertEqual(result.state, 'authenticated')
        self.assertEqual(self.now, 0)

    async def test_explicit_logout_still_requires_stable_evidence(self):
        result = await self.settle(lambda: AuthState('logged_out', '持续登录入口'))
        self.assertEqual(result.state, 'logged_out')
        self.assertGreaterEqual(self.now, .2)

    async def test_initial_login_still_waits_for_stable_account(self):
        result = await self.settle(lambda: AuthState('authenticated', '账号已出现'), stable=True)
        self.assertEqual(result.state, 'authenticated')
        self.assertGreaterEqual(self.now, .2)

    async def test_unconfirmed_component_cannot_become_authenticated(self):
        result = await self.settle(lambda: AuthState('pending', '组件加载中'), timeout=.12)
        self.assertEqual(result.state, 'pending')
        self.assertLessEqual(self.now, .120001)

    async def test_verification_during_fast_wait_stops_immediately(self):
        def status():
            return (AuthState('verification', '需要人工验证') if self.now >= .03
                    else AuthState('logged_out', '登录占位入口'))
        with self.assertRaises(VerificationRequired):
            await self.settle(status)
        self.assertLess(self.now, .1)

    async def test_stop_interrupts_component_wait(self):
        def status():
            self.login.s.scheduler.check_stop.side_effect = Cancelled('用户停止')
            return AuthState('pending', '加载中')
        with self.assertRaises(Cancelled):
            await self.settle(status)

    async def test_challenge_arriving_during_last_slow_sample_is_rechecked(self):
        calls = 0
        def status():
            nonlocal calls
            calls += 1
            self.now = 2
            return AuthState('pending', '旧采样') if calls == 1 else AuthState('verification', '新验证弹层')
        with self.assertRaisesRegex(VerificationRequired, '新验证弹层'):
            await self.settle(status)
        self.assertEqual(calls, 2)
