# -*- coding: utf-8 -*-
"""
tests.test_fetchers
~~~~~~~~~~~~~~~~~~~~~
Test the LaTeX-On-HTTP file fetchers.

:copyright: (c) 2019 Yoan Tournade.
:license: AGPL, see LICENSE for more details.
"""

import pytest
import requests


def test_remote_resource_fetching_is_disabled(latex_on_http_api_url):
    r = requests.post(
        latex_on_http_api_url + "/builds/sync",
        json={
            "resources": [
                {
                    "url": "https://raw.githubusercontent.com/facebook/thefacebook/master/presentation.tex"
                }
            ],
        },
    )
    assert r.status_code == 400
    response_payload = r.json()
    assert response_payload == {
        "error": "REMOTE_RESOURCES_DISABLED",
        "unsupported_resource_types": ["url/file"],
    }
