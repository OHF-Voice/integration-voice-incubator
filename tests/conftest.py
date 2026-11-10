"""Home Assistant's own test fixtures (pytest-homeassistant-custom-component),
with this repository's integration loadable.

    uv venv -p 3.14 && uv pip install -r requirements_test.txt && .venv/bin/pytest
"""

from pathlib import Path

import pytest

COMPONENTS = Path(__file__).resolve().parent.parent / "custom_components"


@pytest.fixture(autouse=True)
def voice_incubator_integration(enable_custom_integrations: None) -> None:
    """Make custom_components.voice_incubator loadable: the test package's
    custom_components, which Home Assistant imports, gets this one's too."""
    import custom_components  # pylint: disable=import-outside-toplevel

    if str(COMPONENTS) not in list(custom_components.__path__):
        custom_components.__path__.append(str(COMPONENTS))


@pytest.fixture(autouse=True)
async def core(hass) -> None:
    """Home Assistant's own component, which a running Home Assistant always
    has set up first: the conversation component's default agent reads which
    entities are exposed from it."""
    from homeassistant.setup import async_setup_component  # pylint: disable=import-outside-toplevel

    assert await async_setup_component(hass, "homeassistant", {})
