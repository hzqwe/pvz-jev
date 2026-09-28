"""Process-wide Windows input ownership, independent of any game or input API.

The OS mutex closes automatically when a controller dies. The local registry is
also necessary: Windows mutexes otherwise permit another object on the owning
thread to recursively acquire the same named mutex.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading


class InputOwnershipError(RuntimeError):
    pass


_registry_lock = threading.Lock()
_owners: dict[str, 'GameInputOwner'] = {}


class GameInputOwner:
    def __init__(self, game_pid: int, namespace: str = 'PvZJevInput'):
        if not isinstance(game_pid, int) or game_pid <= 0:
            raise ValueError('Input ownership requires a positive game PID')
        self.game_pid = game_pid
        self.controller_pid = os.getpid()
        self.name = f'Global\\{namespace}-game-{game_pid}'
        self._handle = None
        self._thread = None
        self._kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self._kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel.CreateMutexW.restype = wintypes.HANDLE
        self._kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._kernel.ReleaseMutex.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL

    @property
    def held(self) -> bool:
        return (self._handle is not None and self.controller_pid == os.getpid()
                and self._thread == threading.get_ident())

    def acquire(self) -> bool:
        if self.held:
            return True
        with _registry_lock:
            if self.name in _owners:
                return False
            handle = self._kernel.CreateMutexW(None, False, self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            status = self._kernel.WaitForSingleObject(handle, 0)
            if status not in (0, 0x80):  # acquired or abandoned by a dead owner
                self._kernel.CloseHandle(handle)
                if status == 0x102:
                    return False
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle, self._thread = handle, threading.get_ident()
            _owners[self.name] = self
            return True

    def check(self, game_pid: int) -> None:
        if not self.held or game_pid != self.game_pid:
            raise InputOwnershipError(f'No input ownership for game PID {game_pid}')

    def release(self) -> None:
        if self._handle is None:
            return
        if not self.held:
            raise InputOwnershipError('Input ownership must be released by its owning thread')
        with _registry_lock:
            if not self._kernel.ReleaseMutex(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._kernel.CloseHandle(self._handle)
            _owners.pop(self.name, None)
            self._handle = self._thread = None


class OwnedClicker:
    """Keep every existing Clicker action behind the same ownership check."""
    _actions = frozenset(('click_client', 'click_card', 'click_grid', 'cancel_seed',
                          'click_shovel', 'shovel_hotkey', 'pick_and_place'))

    def __init__(self, clicker, owner: GameInputOwner):
        self._clicker = clicker
        self._owner = owner

    @property
    def win(self):
        return self._clicker.win

    @win.setter
    def win(self, value):
        self._clicker.win = value

    def __getattr__(self, name):
        value = getattr(self._clicker, name)
        if name not in self._actions:
            return value
        def guarded(*args, **kwargs):
            self._owner.check(self._clicker.win.pid)
            return value(*args, **kwargs)
        return guarded
