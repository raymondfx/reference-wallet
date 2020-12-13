import os
import importlib
import pytest

from libra import testnet, identifier

from ..validator_client import ValidatorClient
from ..vasp_proxy import VaspProxy

# TBD: Manage properly all these globals
STAGING_URL = os.getenv("BASE_URL", "https://staging.lrw.demo.firstdag.com/api")
CHAIN_ID = testnet.CHAIN_ID
HRP = identifier.HRPS[CHAIN_ID.to_int()]


@pytest.fixture
def validator():
    return ValidatorClient.create(STAGING_URL)


@pytest.fixture
def vasp_proxy(pytestconfig) -> VaspProxy:
    vasp_proxy = pytestconfig.hook.pytest_create_vasp_proxy()
    vasp_proxy_module_path = pytestconfig.getoption("vasp_proxy_module", None)
    vasp_proxy_class_name = pytestconfig.getoption("vasp_proxy_class", None)

    if vasp_proxy and not vasp_proxy_module_path and not vasp_proxy_module_path:
        print(f"VASP Validator Plugin: proxy object {vasp_proxy} created using hooks")
        return vasp_proxy

    if not vasp_proxy_module_path:
        raise ValueError(
            "VASP proxy module not specified. Use --vasp-proxy-module option or ensure "
            "the hook function pytest_create_vasp_proxy is registered before the tests"
        )

    if not vasp_proxy_module_path:
        raise ValueError(
            "VASP proxy module not specified. Use --vasp-proxy-module option or ensure "
            "the hook function pytest_create_vasp_proxy is registered before the tests"
        )

    vasp_proxy_module = importlib.import_module(vasp_proxy_module_path)
    vasp_proxy_class = getattr(vasp_proxy_module, vasp_proxy_class_name)
    vasp_proxy = vasp_proxy_class()
    print(
        f"VASP Validator Plugin: proxy object {vasp_proxy} created "
        f"using command line arguments"
    )

    return vasp_proxy
