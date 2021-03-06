#!/usr/bin/env python2
from __future__ import print_function
"""
opy_.py - TODO: This could just be opyc.
"""

import os
import sys

from core import error
from core.pyerror import log

from opy import opy_main

# TODO: move to quick ref?
_OPY_USAGE = 'Usage: opy_ MAIN [OPTION]... [ARG]...'


def AppBundleMain(argv):
  b = os.path.basename(argv[0])
  main_name, ext = os.path.splitext(b)

  if main_name in ('opy_', 'opy') and ext:  # opy_.py or opy.ovm
    try:
      first_arg = argv[1]
    except IndexError:
      raise error.Usage('Missing required applet name.')

    main_name = first_arg
    argv0 = argv[1]
    main_argv = argv[2:]
  else:
    argv0 = argv[0]
    main_argv = argv[1:]

  if main_name == 'opyc':
    return opy_main.OpyCommandMain(main_argv)

  else:
    raise error.Usage('Invalid applet name %r.' % main_name)


def main(argv):
  try:
    sys.exit(AppBundleMain(argv))
  except error.Usage as e:
    #print(_OPY_USAGE, file=sys.stderr)
    log('opy: %s', e.msg)
    sys.exit(2)
  except RuntimeError as e:
    log('FATAL: %s', e)
    sys.exit(1)


if __name__ == '__main__':
  # NOTE: This could end up as opy.InferTypes(), opy.GenerateCode(), etc.
  if os.getenv('CALLGRAPH') == '1':
    from opy import callgraph
    callgraph.Walk(main, sys.modules)
  else:
    main(sys.argv)
