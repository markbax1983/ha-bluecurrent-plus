"""Charge card selection (used by the 'Start session' button)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NO_CARD_UID
from .coordinator import BlueCurrentConfigEntry, BlueCurrentCoordinator
from .entity import BlueCurrentEntity

NO_CARD_LABELS = {"nl": "Zonder laadpas", "en": "No charge card"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueCurrentConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ChargeCardSelect(coordinator, evse_id) for evse_id in coordinator.data.charge_points)


def card_label(uid: str, card: dict) -> str:
    name = card.get("name") or ""
    token = card.get("id") or ""
    parts = [p for p in (name, token) if p]
    label = " - ".join(parts) if parts else uid
    return f"{label} ({uid})" if parts else label


class ChargeCardSelect(BlueCurrentEntity, SelectEntity):
    """Which card 'Start session' uses."""

    def __init__(self, coordinator: BlueCurrentCoordinator, evse_id: str) -> None:
        super().__init__(coordinator, evse_id, "charge_card")

    @property
    def _no_card_label(self) -> str:
        return NO_CARD_LABELS.get(self.hass.config.language.split("-")[0], NO_CARD_LABELS["en"])

    def _labels(self) -> dict[str, str]:
        labels = {self._no_card_label: NO_CARD_UID}
        for uid, card in self.coordinator.data.cards.items():
            labels[card_label(uid, card)] = uid
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

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"uid": self.cp.card_uid}

    async def async_select_option(self, option: str) -> None:
        self.coordinator.async_select_card(self.evse_id, self._labels()[option])
