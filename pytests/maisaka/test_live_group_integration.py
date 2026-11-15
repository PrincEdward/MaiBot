from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import tomlkit

from src.chat.message_receive.chat_manager import chat_manager
from src.config.config import global_config
from src.config.live_defaults import DEFAULT_LIVE_CHAT_PROMPT, LIVE_CHAT_PROMPTS
from src.config.official_configs import ChatReplyStyleConfig, PersonalityProfile
from src.config.personality_service import persist_switch
from src.maisaka.memory.heuristic_injector import HeuristicMemoryContext, HeuristicMemoryInjector
from src.maisaka.scenes.factory import get_prompt_profile, get_scene
from src.services.memory_service import MemoryHit, MemorySearchResult


def session(platform, sid, group=True):
    return SimpleNamespace(platform=platform, session_id=sid, is_group_session=group, group_id="123" if group else "", user_id="")


def test_live_prompt_keeps_native_group_scene_and_other_prompts(monkeypatch):
    rooms = {"live": session("bilibili", "live"), "qq": session("qq", "qq"), "private": session("bilibili", "private", False)}
    monkeypatch.setattr(chat_manager, "get_session_by_session_id", rooms.get)
    assert get_prompt_profile(True, "live").attention_text() == DEFAULT_LIVE_CHAT_PROMPT
    assert get_prompt_profile(True, "qq") is get_scene(True).prompts
    assert get_prompt_profile(False, "private") is get_scene(False).prompts
    assert get_prompt_profile(True, "live").select_chat_reply_template(self_reply=False, mai_replier=False) == "reply_group"
    assert set(LIVE_CHAT_PROMPTS) == {"zh-CN", "en-US", "ja-JP"}


def test_old_profile_import_and_switch_preserve_live_prompt(tmp_path):
    old = dict(name="old", personality="p", behavior_style="b", reply_style="r", group_chat_prompt="group", private_chat_prompts="private")
    assert PersonalityProfile(**old).live_chat_prompt == DEFAULT_LIVE_CHAT_PROMPT
    current = dict(old, name="current", live_chat_prompt="current live")
    target = dict(old, name="target", live_chat_prompt="target live")
    path = tmp_path / "bot_config.toml"
    path.write_text(tomlkit.dumps({"personality": {"selected_profile": "current", "profiles": [current, target]},
        "chat": {"reply_style": dict(ChatReplyStyleConfig().model_dump(), live_chat_prompt="edited current live")}}), encoding="utf-8")
    assert persist_switch(path, "target")
    saved = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert saved["chat"]["reply_style"]["live_chat_prompt"] == "target live"
    assert saved["personality"]["profiles"][0]["live_chat_prompt"] == "edited current live"


@pytest.mark.asyncio
async def test_native_shared_summaries_reach_heuristic_without_global_sharing(monkeypatch):
    import src.maisaka.memory.heuristic_injector as module
    current = session("bilibili", "live")
    injector = object.__new__(HeuristicMemoryInjector)
    hit = MemoryHit("一起聊过的游戏", source="chat_summary:qq", hash_value="shared")
    private = MemoryHit("private", source="chat_summary:private", hash_value="private")
    unknown = MemoryHit("unknown", hash_value="unknown")
    person = MemoryHit("person", source="person_fact:other", hash_value="person")
    monkeypatch.setattr(module.memory_service, "search", AsyncMock(return_value=MemorySearchResult(hits=[hit, private, unknown, person])))
    monkeypatch.setattr(injector, "_shared_group_session_ids", lambda _: frozenset({"live", "qq"}))
    monkeypatch.setattr(injector, "_resolve_hit_sources_by_hash", AsyncMock(return_value={}))
    monkeypatch.setattr(injector, "_is_chat_memory_allowed", lambda source, current: source == current.session_id)
    context = HeuristicMemoryContext(current, [])
    result = await injector._search_related_memory("游戏", context)
    assert result == [hit]
    assert module.memory_service.search.call_args.kwargs["respect_filter"] is True
    assert module.memory_service.search.call_args.kwargs["chat_id"] == "live"


def test_explicit_sharing_never_admits_private_sources(monkeypatch):
    import src.maisaka.memory.heuristic_injector as module
    rooms = {"live": session("bilibili", "live"), "qq": session("qq", "qq"), "private": session("qq", "private", False)}
    monkeypatch.setattr(module.AMemorixConfigUtils, "get_shared_memory_session_ids", lambda _: set(rooms))
    monkeypatch.setattr(chat_manager, "get_existing_session_by_session_id", rooms.get)
    assert HeuristicMemoryInjector._shared_group_session_ids(rooms["live"]) == frozenset({"live", "qq"})
    assert HeuristicMemoryInjector._shared_group_session_ids(rooms["private"]) == frozenset()
