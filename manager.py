"""Sleeper fantasy football plugin for LEDMatrix."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.plugin_system.base_plugin import BasePlugin


Color = Tuple[int, int, int]


class SleeperFantasyFootballPlugin(BasePlugin):
    """Display Sleeper fantasy football matchups and standings."""

    API_BASE_URL = "https://api.sleeper.app/v1"

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager: Any,
        cache_manager: Any,
        plugin_manager: Any,
    ) -> None:
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self._apply_config(config)
        self.session = self._build_session()
        self.status_code = "init"
        self.status_message = "Starting"
        self.last_update = 0.0
        self.league_name = ""
        self.week = 0
        self.season_used = ""
        self.resolved_league_id = ""
        self.favorite_roster_id: Optional[int] = None
        self.cards: List[Dict[str, Any]] = []
        self.card_index = 0
        self.last_card_rotation = 0.0

    def _apply_config(self, config: Dict[str, Any]) -> None:
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.league_id = str(self.config.get("league_id", "") or "").strip()
        self.username = str(self.config.get("username", "") or "").strip()
        self.season = str(self.config.get("season", "") or "").strip()
        self.roster_id = int(self.config.get("roster_id", 0) or 0)
        self.favorite_team_name = str(self.config.get("favorite_team_name", "") or "").strip().lower()
        self.display_mode = str(self.config.get("display_mode", "auto") or "auto").lower()
        if self.display_mode not in {"matchup", "standings", "auto"}:
            self.display_mode = "auto"
        self.show_all_matchups = bool(self.config.get("show_all_matchups", False))
        self.max_standings_teams = max(2, min(8, int(self.config.get("max_standings_teams", 4))))
        self.cache_seconds = max(60, int(self.config.get("cache_seconds", 300)))
        self.display_duration = float(self.config.get("display_duration", 20))
        self.card_rotation_seconds = max(2.0, float(self.config.get("card_rotation_seconds", 5)))
        self.request_timeout = max(2, int(self.config.get("request_timeout", 8)))
        self.primary_color = self._normalize_color(self.config.get("primary_color"), (255, 255, 255))
        self.secondary_color = self._normalize_color(self.config.get("secondary_color"), (130, 210, 255))
        self.accent_color = self._normalize_color(self.config.get("accent_color"), (60, 255, 150))

    @staticmethod
    def _normalize_color(value: Any, fallback: Color) -> Color:
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                return tuple(max(0, min(255, int(channel))) for channel in value)
            except (TypeError, ValueError):
                return fallback
        return fallback

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({"User-Agent": "LEDMatrix/SleeperFantasyFootballPlugin"})
        return session

    def _get_json(self, path: str) -> Any:
        url = f"{self.API_BASE_URL}{path}"
        response = self.session.get(url, timeout=self.request_timeout)
        response.raise_for_status()
        return response.json()

    def _get_cached_json(self, cache_key: str, path: str) -> Any:
        cached = self.cache_manager.get(cache_key, max_age=self.cache_seconds)
        if cached is not None:
            return cached
        payload = self._get_json(path)
        self.cache_manager.set(cache_key, payload, ttl=self.cache_seconds)
        return payload

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_float(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _team_name(user: Optional[Dict[str, Any]], roster_id: int) -> str:
        if isinstance(user, dict):
            metadata = user.get("metadata") if isinstance(user.get("metadata"), dict) else {}
            for key in ("team_name", "display_name"):
                text = str(metadata.get(key, "") or "").strip()
                if text:
                    return text
            for key in ("display_name", "username"):
                text = str(user.get(key, "") or "").strip()
                if text:
                    return text
        return f"Roster {roster_id}"

    @staticmethod
    def _record(settings: Dict[str, Any]) -> str:
        wins = int(settings.get("wins", 0) or 0)
        losses = int(settings.get("losses", 0) or 0)
        ties = int(settings.get("ties", 0) or 0)
        return f"{wins}-{losses}" if ties == 0 else f"{wins}-{losses}-{ties}"

    @staticmethod
    def _season_points(settings: Dict[str, Any]) -> float:
        whole = float(settings.get("fpts", 0) or 0)
        decimal = float(settings.get("fpts_decimal", 0) or 0) / 100.0
        return whole + decimal

    def _resolve_user_id(self) -> str:
        if not self.username:
            return ""
        payload = self._get_cached_json(
            f"{self.plugin_id}_user_{self.username.lower()}",
            f"/user/{self.username}",
        )
        if isinstance(payload, dict):
            return str(payload.get("user_id", "") or "").strip()
        return ""

    def _resolve_context(self) -> Tuple[str, str, int]:
        state = self._get_cached_json(f"{self.plugin_id}_state_nfl", "/state/nfl")
        season = self.season
        week = 0
        if isinstance(state, dict):
            season = season or str(state.get("league_season") or state.get("season") or "")
            week = int(state.get("display_week") or state.get("week") or state.get("leg") or 0)

        league_id = self.league_id
        if not league_id:
            user_id = self._resolve_user_id()
            if user_id and season:
                leagues = self._get_cached_json(
                    f"{self.plugin_id}_leagues_{user_id}_{season}",
                    f"/user/{user_id}/leagues/nfl/{season}",
                )
                if isinstance(leagues, list) and leagues:
                    in_season = [league for league in leagues if league.get("status") == "in_season"]
                    league = in_season[0] if in_season else leagues[0]
                    league_id = str(league.get("league_id", "") or "").strip()

        return league_id, season, max(1, week)

    def _build_roster_maps(
        self,
        users_payload: Any,
        rosters_payload: Any,
    ) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        users_by_id = {}
        if isinstance(users_payload, list):
            users_by_id = {
                str(user.get("user_id", "")): user
                for user in users_payload
                if isinstance(user, dict)
            }

        rosters: Dict[int, Dict[str, Any]] = {}
        if not isinstance(rosters_payload, list):
            return rosters, {}

        for roster in rosters_payload:
            if not isinstance(roster, dict):
                continue
            rid = self._coerce_int(roster.get("roster_id"))
            if rid is None:
                continue
            owner_id = str(roster.get("owner_id", "") or "")
            user = users_by_id.get(owner_id)
            settings = roster.get("settings") if isinstance(roster.get("settings"), dict) else {}
            rosters[rid] = {
                "roster_id": rid,
                "owner_id": owner_id,
                "user": user,
                "name": self._team_name(user, rid),
                "record": self._record(settings),
                "wins": int(settings.get("wins", 0) or 0),
                "losses": int(settings.get("losses", 0) or 0),
                "ties": int(settings.get("ties", 0) or 0),
                "points_for": self._season_points(settings),
            }
        return rosters, users_by_id

    def _resolve_favorite_roster(self, rosters: Dict[int, Dict[str, Any]], users_by_id: Dict[str, Dict[str, Any]]) -> Optional[int]:
        if self.roster_id and self.roster_id in rosters:
            return self.roster_id

        user_id = ""
        if self.username:
            try:
                user_id = self._resolve_user_id()
            except Exception:
                user_id = ""
        if user_id:
            for rid, roster in rosters.items():
                if roster.get("owner_id") == user_id:
                    return rid

        if self.favorite_team_name:
            needle = self.favorite_team_name
            for rid, roster in rosters.items():
                if needle in str(roster.get("name", "")).lower():
                    return rid
                user = roster.get("user") if isinstance(roster.get("user"), dict) else {}
                if needle in str(user.get("username", "")).lower():
                    return rid
                if needle in str(user.get("display_name", "")).lower():
                    return rid
        return None

    def _build_matchup_cards(self, matchups_payload: Any, rosters: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(matchups_payload, list):
            return []

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for item in matchups_payload:
            if not isinstance(item, dict):
                continue
            mid = self._coerce_int(item.get("matchup_id"))
            rid = self._coerce_int(item.get("roster_id"))
            if mid is None or rid is None:
                continue
            grouped.setdefault(mid, []).append(
                {
                    "roster_id": rid,
                    "points": self._coerce_float(item.get("points")),
                    "name": rosters.get(rid, {}).get("name", f"Roster {rid}"),
                    "record": rosters.get(rid, {}).get("record", ""),
                }
            )

        cards = []
        for matchup_id, teams in sorted(grouped.items()):
            if len(teams) < 2:
                continue
            teams = sorted(teams, key=lambda team: team["points"], reverse=True)
            roster_ids = {team["roster_id"] for team in teams}
            is_favorite = self.favorite_roster_id in roster_ids if self.favorite_roster_id else False
            if not self.show_all_matchups and self.favorite_roster_id and not is_favorite:
                continue
            cards.append(
                {
                    "type": "matchup",
                    "title": f"WEEK {self.week} MATCHUP",
                    "matchup_id": matchup_id,
                    "teams": teams[:2],
                    "is_favorite": is_favorite,
                }
            )

        return cards

    def _build_standings_cards(self, rosters: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        standings = sorted(
            rosters.values(),
            key=lambda roster: (
                roster.get("wins", 0),
                -roster.get("losses", 0),
                roster.get("points_for", 0.0),
            ),
            reverse=True,
        )
        if not standings:
            return []

        rows = standings[: self.max_standings_teams]
        return [
            {
                "type": "standings",
                "title": "FANTASY STANDINGS",
                "teams": rows,
            }
        ]

    def _build_cards(self, league_id: str, season: str, week: int) -> List[Dict[str, Any]]:
        league = self._get_cached_json(f"{self.plugin_id}_league_{league_id}", f"/league/{league_id}")
        users = self._get_cached_json(f"{self.plugin_id}_users_{league_id}", f"/league/{league_id}/users")
        rosters_payload = self._get_cached_json(f"{self.plugin_id}_rosters_{league_id}", f"/league/{league_id}/rosters")

        rosters, users_by_id = self._build_roster_maps(users, rosters_payload)
        self.favorite_roster_id = self._resolve_favorite_roster(rosters, users_by_id)
        self.league_name = str(league.get("name", "") or "Sleeper League") if isinstance(league, dict) else "Sleeper League"
        self.resolved_league_id = league_id
        self.season_used = season
        self.week = week

        standings_cards = self._build_standings_cards(rosters)
        if self.display_mode == "standings":
            return standings_cards

        matchups = self._get_cached_json(
            f"{self.plugin_id}_matchups_{league_id}_{week}",
            f"/league/{league_id}/matchups/{week}",
        )
        matchup_cards = self._build_matchup_cards(matchups, rosters)

        if self.display_mode == "matchup":
            return matchup_cards or standings_cards
        return matchup_cards + standings_cards

    def update(self) -> None:
        if not self.enabled:
            return

        try:
            if not self.league_id and not self.username:
                self.cards = []
                self.status_code = "missing_config"
                self.status_message = "Add Sleeper league ID"
                return

            league_id, season, week = self._resolve_context()
            if not league_id:
                self.cards = []
                self.status_code = "missing_config"
                self.status_message = "Add Sleeper league ID"
                return

            cards = self._build_cards(league_id, season, week)
            self.cards = cards
            self.status_code = "ok" if cards else "empty"
            self.status_message = "Ready" if cards else "No matchup data"
            self.last_update = time.time()
            self.card_index = min(self.card_index, max(0, len(self.cards) - 1))
        except Exception as exc:
            self.logger.warning("Sleeper update failed: %s", exc)
            if self.cards:
                self.status_code = "stale"
                self.status_message = "Using cached fantasy data"
            else:
                self.status_code = "api_error"
                self.status_message = f"Sleeper error: {str(exc)[:28]}"

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        try:
            project_root = Path(__file__).resolve().parents[2]
            font_path = project_root / "assets" / "fonts" / "4x6-font.ttf"
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size)
        except Exception:
            pass
        return ImageFont.load_default()

    @staticmethod
    def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        text = str(text or "")
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return text
        ellipsis = "."
        while text and draw.textbbox((0, 0), text + ellipsis, font=font)[2] > max_width:
            text = text[:-1]
        return text + ellipsis if text else ""

    @staticmethod
    def _right_text(draw: ImageDraw.ImageDraw, x_right: int, y: int, text: str, font: ImageFont.ImageFont, fill: Color) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((x_right - (bbox[2] - bbox[0]), y), text, font=font, fill=fill)

    def _render_status(self, width: int, height: int) -> Image.Image:
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        small = self._load_font(6)
        draw.text((2, 2), "SLEEPER FF", font=small, fill=self.accent_color)
        draw.text((2, 12), self._truncate(draw, self.status_message, small, width - 4), font=small, fill=self.primary_color)
        hint = "set league_id" if self.status_code == "missing_config" else self.status_code.upper()
        draw.text((2, 22), self._truncate(draw, hint, small, width - 4), font=small, fill=self.secondary_color)
        return image

    def _render_matchup(self, card: Dict[str, Any], width: int, height: int) -> Image.Image:
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        small = self._load_font(6)
        teams = card.get("teams", [])
        draw.text((2, 0), self._truncate(draw, str(card.get("title", "MATCHUP")), small, width - 4), font=small, fill=self.secondary_color)
        draw.line((0, 8, width - 1, 8), fill=self.accent_color)

        y_positions = [10, 21] if height >= 32 else [9, 17]
        for idx, team in enumerate(teams[:2]):
            y = y_positions[idx]
            is_leader = idx == 0 and teams[0].get("points", 0.0) != teams[1].get("points", 0.0)
            color = self.accent_color if is_leader else self.primary_color
            name = self._truncate(draw, str(team.get("name", "")), small, max(10, width - 48))
            score = f"{float(team.get('points', 0.0)):.1f}"
            draw.text((2, y), name, font=small, fill=color)
            self._right_text(draw, width - 2, y, score, small, color)
        return image

    def _render_standings(self, card: Dict[str, Any], width: int, height: int) -> Image.Image:
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        small = self._load_font(6)
        draw.text((2, 0), self._truncate(draw, str(card.get("title", "STANDINGS")), small, width - 4), font=small, fill=self.secondary_color)
        draw.line((0, 8, width - 1, 8), fill=self.accent_color)

        row_height = 7 if height <= 32 else 8
        for idx, team in enumerate(card.get("teams", [])[: self.max_standings_teams]):
            y = 10 + idx * row_height
            if y > height - 6:
                break
            rank = f"{idx + 1}."
            record = str(team.get("record", ""))
            color = self.accent_color if team.get("roster_id") == self.favorite_roster_id else self.primary_color
            name_width = max(10, width - 52)
            name = self._truncate(draw, str(team.get("name", "")), small, name_width)
            draw.text((2, y), rank, font=small, fill=self.secondary_color)
            draw.text((14, y), name, font=small, fill=color)
            self._right_text(draw, width - 2, y, record, small, self.secondary_color)
        return image

    def _current_card(self) -> Optional[Dict[str, Any]]:
        if not self.cards:
            return None
        current_time = time.time()
        if current_time - self.last_card_rotation >= self.card_rotation_seconds:
            self.card_index = (self.card_index + 1) % len(self.cards)
            self.last_card_rotation = current_time
        return self.cards[self.card_index]

    def _render_card(self, card: Dict[str, Any], width: int, height: int) -> Image.Image:
        if card.get("type") == "standings":
            return self._render_standings(card, width, height)
        return self._render_matchup(card, width, height)

    def display(self, force_clear: bool = False) -> None:
        if force_clear:
            self.display_manager.clear()

        width = getattr(self.display_manager, "width", self.display_manager.matrix.width)
        height = getattr(self.display_manager, "height", self.display_manager.matrix.height)
        card = self._current_card()
        image = self._render_card(card, width, height) if card else self._render_status(width, height)
        self.display_manager.image = image
        self.display_manager.update_display()

    def get_vegas_content(self) -> Optional[List[Image.Image]]:
        width = getattr(self.display_manager, "width", self.display_manager.matrix.width)
        height = getattr(self.display_manager, "height", self.display_manager.matrix.height)
        if not self.cards:
            return [self._render_status(width, height)]
        return [self._render_card(card, width, height) for card in self.cards]

    def get_vegas_content_type(self) -> str:
        return "multi"

    def validate_config(self) -> bool:
        if not super().validate_config():
            return False
        if self.display_mode not in {"matchup", "standings", "auto"}:
            self.logger.error("display_mode must be matchup, standings, or auto")
            return False
        return True

    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        super().on_config_change(new_config)
        self._apply_config(new_config)
        self.cards = []
        self.status_code = "config_changed"
        self.status_message = "Reloading Sleeper"

    def get_info(self) -> Dict[str, Any]:
        info = super().get_info()
        info.update(
            {
                "status_code": self.status_code,
                "status_message": self.status_message,
                "league_id": self.resolved_league_id,
                "league_name": self.league_name,
                "season": self.season_used,
                "week": self.week,
                "favorite_roster_id": self.favorite_roster_id,
                "cards": len(self.cards),
                "last_update": self.last_update,
            }
        )
        return info
