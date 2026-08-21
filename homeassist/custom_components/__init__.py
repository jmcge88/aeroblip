"""Marker so ``custom_components`` resolves here during tests.

pytest-homeassistant-custom-component ships its own regular
``custom_components`` package, and Python prefers any regular package over
a namespace one - without this file, test runs would resolve
``custom_components`` to the test library's copy and never find aeroblip.
Installs are unaffected: only the ``aeroblip`` subdirectory is deployed.
"""
