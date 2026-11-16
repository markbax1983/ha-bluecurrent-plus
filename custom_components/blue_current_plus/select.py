"""Charge card selection (used by the 'Start session' button)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NO_CARD_UID
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator
from .entity import BlueCurrentEntity

NO_CARD_LABELS = {"nl": "Zonder laadpas", "en": "No charge card"}
CARD_LABELS = {"nl": "Laadpas", "en": "Charge card"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ChargeCardSelect(coordinator, evse_id) for evse_id in coordinator.data.charge_points)


def mask(value: str, keep: int = 4) -> str:
    """Show only the last `keep` characters: enough to tell cards apart."""
    value = str(value)
    return value if len(value) <= keep else f"\u2026{value[-keep:]}"


def card_label(card: dict, fallback: str = "Charge card") -> str:
    """Human label for a card WITHOUT the RFID UID or the full EMP number.

    The UID is what many charge points check: anyone who knows it can clone the
    card. Labels end up in the state machine, recorder and backups, so they
    only carry the card name and the last characters of the EMP number.
    """
    name = str(card.get("name") or "").strip()
    token = str(card.get("id") or "").strip()
    if name and token:
        return f"{name} ({mask(token)})"
    if name:
        return name
    if token:
        return f"{fallback} {mask(token)}"
    return fallback


class ChargeCardSelect(BlueCurrentEntity, SelectEntity):
    """Which card 'Start session' uses."""

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str) -> None:
        super().__init__(coordinator, evse_id, "charge_card")

    @property
    def _lang(self) -> str:
        return self.hass.config.language.split("-")[0]

    @property
    def _no_card_label(self) -> str:
        return NO_CARD_LABELS.get(self._lang, NO_CARD_LABELS["en"])

    def _labels(self) -> dict[str, str]:
        """Label -> UID. The mapping stays inside the integration."""
        fallback = CARD_LABELS.get(self._lang, CARD_LABELS["en"])
        labels = {self._no_card_label: NO_CARD_UID}
        for uid, card in self.coordinator.data.cards.items():
            label = base = card_label(card, fallback)
            n = 2
            while label in labels:
                label = f"{base} #{n}"
                n += 1
            labels[label] = uid
        return labels

    @property
    def options(self) -> list[str]:
        return list(self._labels())

    @property
    def current_option(self) -> str | None:
        uid = self.cp.card_uid
        for label, value in self._labels().items():
            if value == uid:
                return label
        return self._no_card_label if uid == NO_CARD_UID else None

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_select_card(self.evse_id, self._labels()[option])
