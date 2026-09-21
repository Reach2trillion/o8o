# -*- coding: utf-8 -*-
from . import models
from . import wizard
from . import report


def post_init_hook(env):
    """Schedule the Telegram report crons at sensible local times of the main company."""
    env['checkinme.telegram']._align_report_crons()
