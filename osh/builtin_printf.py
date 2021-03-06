#!/usr/bin/env python2
"""
builtin_printf.py
"""
from __future__ import print_function

import time as time_  # avoid name conflict

from _devbuild.gen import arg_types
from _devbuild.gen.id_kind_asdl import Id, Kind
from _devbuild.gen.runtime_asdl import (
    cmd_value__Argv, value_e, value__Str, value, lvalue_e
)
from _devbuild.gen.syntax_asdl import (
    printf_part, printf_part_e, printf_part_t, printf_part__Literal,
    printf_part__Percent, source, Token,
)
from _devbuild.gen.types_asdl import lex_mode_e, lex_mode_t

from asdl import runtime
from core import alloc
from core import error
from core.pyerror import e_usage, e_die, p_die, log
from core import state
from core import ui
from core import vm
from frontend import flag_spec
from frontend import consts
from frontend import match
from frontend import reader
from mycpp import mylib
from osh import word_compile
from qsn_ import qsn

import posix_ as posix

from typing import Dict, List, TYPE_CHECKING, cast

if TYPE_CHECKING:
  from core import optview
  from core.state import Mem
  from core.ui import ErrorFormatter
  from frontend.lexer import Lexer
  from frontend.parse_lib import ParseContext
  from osh.sh_expr_eval import ArithEvaluator

_ = log


class _FormatStringParser(object):
  """
  Grammar:

    width         = Num | Star
    precision     = Dot (Num | Star | Zero)?
    fmt           = Percent (Flag | Zero)* width? precision? (Type | Time)
    part          = Char_* | Format_EscapedPercent | fmt
    printf_format = part* Eof_Real   # we're using the main lexer

  Maybe: bash also supports %(strftime)T
  """
  def __init__(self, lexer):
    # type: (Lexer) -> None
    self.lexer = lexer

  def _Next(self, lex_mode):
    # type: (lex_mode_t) -> None
    """Set the next lex state, but don't actually read a token.

    We need this for proper interactive parsing.
    """
    self.cur_token = self.lexer.Read(lex_mode)
    self.token_type = self.cur_token.id
    self.token_kind = consts.GetKind(self.token_type)

  def _ParseFormatStr(self):
    # type: () -> printf_part_t
    """ fmt production """
    self._Next(lex_mode_e.PrintfPercent)  # move past %

    part = printf_part.Percent()
    while self.token_type in (Id.Format_Flag, Id.Format_Zero):
      # space and + could be implemented
      flag = self.cur_token.val
      if flag in '# +':
        p_die("osh printf doesn't support the %r flag", flag, token=self.cur_token)

      part.flags.append(self.cur_token)
      self._Next(lex_mode_e.PrintfPercent)

    if self.token_type in (Id.Format_Num, Id.Format_Star):
      part.width = self.cur_token
      self._Next(lex_mode_e.PrintfPercent)

    if self.token_type == Id.Format_Dot:
      part.precision = self.cur_token
      self._Next(lex_mode_e.PrintfPercent)  # past dot
      if self.token_type in (Id.Format_Num, Id.Format_Star, Id.Format_Zero):
        part.precision = self.cur_token
        self._Next(lex_mode_e.PrintfPercent)

    if self.token_type in (Id.Format_Type, Id.Format_Time):
      part.type = self.cur_token

      # ADDITIONAL VALIDATION outside the "grammar".
      if part.type.val in 'eEfFgG':
        p_die("osh printf doesn't support floating point", token=part.type)
      # These two could be implemented.  %c needs utf-8 decoding.
      if part.type.val == 'c':
        p_die("osh printf doesn't support single characters (bytes)", token=part.type)

    elif self.token_type == Id.Unknown_Tok:
      p_die('Invalid printf format character', token=self.cur_token)

    else:
      p_die('Expected a printf format character', token=self.cur_token)

    # Do this check AFTER the floating point checks
    if part.precision and part.type.val[-1] not in 'fsT':
      p_die("printf precision can't be specified with type %r" % part.type.val,
            token=part.precision)

    return part

  def Parse(self):
    # type: () -> List[printf_part_t]
    self._Next(lex_mode_e.PrintfOuter)
    parts = []  # type: List[printf_part_t]
    while True:
      if (self.token_kind == Kind.Char or
          self.token_type == Id.Format_EscapedPercent or
          self.token_type == Id.Unknown_Backslash):

        # Note: like in echo -e, we don't fail with Unknown_Backslash here
        # when shopt -u pasre_backslash because it's at runtime rather than
        # parse time.
        # Users should use $'' or the future static printf ${x %.3f}.

        parts.append(printf_part.Literal(self.cur_token))

      elif self.token_type == Id.Format_Percent:
        parts.append(self._ParseFormatStr())

      elif self.token_type == Id.Eof_Real:
        break

      else:
        raise AssertionError()

      self._Next(lex_mode_e.PrintfOuter)

    return parts


class Printf(vm._Builtin):

  def __init__(self, mem, exec_opts, parse_ctx, arith_ev, errfmt):
    # type: (Mem, optview.Exec, ParseContext, ArithEvaluator, ErrorFormatter) -> None
    self.mem = mem
    self.exec_opts = exec_opts
    self.parse_ctx = parse_ctx
    self.arith_ev = arith_ev
    self.errfmt = errfmt
    self.parse_cache = {}  # type: Dict[str, List[printf_part_t]]

    self.shell_start_time = time_.time()  # this object initialized in main()

  def Run(self, cmd_val):
    # type: (cmd_value__Argv) -> int
    """
    printf: printf [-v var] format [argument ...]
    """
    attrs, arg_r = flag_spec.ParseCmdVal('printf', cmd_val)
    arg = arg_types.printf(attrs.attrs)

    fmt, fmt_spid = arg_r.ReadRequired2('requires a format string')
    varargs, spids = arg_r.Rest2()

    #log('fmt %s', fmt)
    #log('vals %s', vals)

    arena = self.parse_ctx.arena
    if fmt in self.parse_cache:
      parts = self.parse_cache[fmt]
    else:
      line_reader = reader.StringLineReader(fmt, arena)
      # TODO: Make public
      lexer = self.parse_ctx._MakeLexer(line_reader)
      parser = _FormatStringParser(lexer)

      with alloc.ctx_Location(arena, source.ArgvWord(fmt_spid)):
        try:
          parts = parser.Parse()
        except error.Parse as e:
          self.errfmt.PrettyPrintError(e)
          return 2  # parse error

      self.parse_cache[fmt] = parts

    if 0:
      print()
      for part in parts:
        part.PrettyPrint()
        print()

    out = []  # type: List[str]
    arg_index = 0
    num_args = len(varargs)
    backslash_c = False

    while True:
      for part in parts:
        UP_part = part
        if part.tag_() == printf_part_e.Literal:
          part = cast(printf_part__Literal, UP_part)
          token = part.token
          if token.id == Id.Format_EscapedPercent:
            s = '%'
          else:
            s = word_compile.EvalCStringToken(token)
          out.append(s)

        elif part.tag_() == printf_part_e.Percent:
          part = cast(printf_part__Percent, UP_part)
          flags = []  # type: List[str]
          if len(part.flags) > 0:
            for flag_token in part.flags:
              flags.append(flag_token.val)

          width = -1  # nonexistent
          if part.width:
            if part.width.id in (Id.Format_Num, Id.Format_Zero):
              width_str = part.width.val
              width_spid = part.width.span_id
            elif part.width.id == Id.Format_Star:
              if arg_index < num_args:
                width_str = varargs[arg_index]
                width_spid = spids[arg_index]
                arg_index += 1
              else:
                width_str = ''  # invalid
                width_spid = runtime.NO_SPID
            else:
              raise AssertionError()

            try:
              width = int(width_str)
            except ValueError:
              if width_spid == runtime.NO_SPID:
                width_spid = part.width.span_id
              self.errfmt.Print_("printf got invalid width %r" % width_str,
                                 span_id=width_spid)
              return 1

          precision = -1  # nonexistent
          if part.precision:
            if part.precision.id == Id.Format_Dot:
              precision_str = '0'
              precision_spid = part.precision.span_id
            elif part.precision.id in (Id.Format_Num, Id.Format_Zero):
              precision_str = part.precision.val
              precision_spid = part.precision.span_id
            elif part.precision.id == Id.Format_Star:
              if arg_index < num_args:
                precision_str = varargs[arg_index]
                precision_spid = spids[arg_index]
                arg_index += 1
              else:
                precision_str = ''
                precision_spid = runtime.NO_SPID
            else:
              raise AssertionError()

            try:
              precision = int(precision_str)
            except ValueError:
              if precision_spid == runtime.NO_SPID:
                precision_spid = part.precision.span_id
              self.errfmt.Print_(
                  'printf got invalid precision %r' % precision_str,
                  span_id=precision_spid)
              return 1

          #log('index=%d n=%d', arg_index, num_args)
          if arg_index < num_args:
            s = varargs[arg_index]
            word_spid = spids[arg_index]
            arg_index += 1
          else:
            s = ''
            word_spid = runtime.NO_SPID

          typ = part.type.val
          if typ == 's':
            if precision >= 0:
              s = s[:precision]  # truncate

          elif typ == 'q':
            s = qsn.maybe_shell_encode(s)

          elif typ == 'b':
            # Process just like echo -e, except \c handling is simpler.

            c_parts = []  # type: List[str]
            lex = match.EchoLexer(s)
            while True:
              id_, tok_val = lex.Next()
              if id_ == Id.Eol_Tok:  # Note: This is really a NUL terminator
                break

              # TODO: add span_id from argv
              tok = Token(id_, runtime.NO_SPID, tok_val)
              p = word_compile.EvalCStringToken(tok)

              # Unusual behavior: '\c' aborts processing!
              if p is None:
                backslash_c = True
                break

              c_parts.append(p)
            s = ''.join(c_parts)

          elif typ in 'diouxX' or part.type.id == Id.Format_Time:
            try:
              d = int(s)
            except ValueError:
              if len(s) >= 1 and s[0] in '\'"':
                # TODO: utf-8 decode s[1:] to be more correct.  Probably
                # depends on issue #366, a utf-8 library.
                # Note: len(s) == 1 means there is a NUL (0) after the quote..
                d = ord(s[1]) if len(s) >= 2 else 0
              elif part.type.id == Id.Format_Time and len(s) == 0 and word_spid == runtime.NO_SPID:
                # Note: No argument means -1 for %(...)T as in Bash Reference
                #   Manual 4.2 "If no argument is specified, conversion behaves
                #   as if -1 had been given."
                d = -1
              else:
                if word_spid == runtime.NO_SPID:
                  # Blame the format string
                  blame_spid = part.type.span_id
                else:
                  blame_spid = word_spid
                self.errfmt.Print_('printf expected an integer, got %r' % s,
                                   span_id=blame_spid)
                return 1

            if typ in 'di':
              s = str(d)
            elif typ in 'ouxX':
              if d < 0:
                e_die("Can't format negative number %d with %%%s",
                      d, typ, span_id=part.type.span_id)
              if typ == 'u':
                s = str(d)
              elif typ == 'o':
                s = mylib.octal(d)
              elif typ == 'x':
                s = mylib.hex_lower(d)
              elif typ == 'X':
                s = mylib.hex_upper(d)

            elif part.type.id == Id.Format_Time:
              # %(...)T

              # Initialize timezone:
              #   `localtime' uses the current timezone information initialized
              #   by `tzset'.  The function `tzset' refers to the environment
              #   variable `TZ'.  When the exported variable `TZ' is present,
              #   its value should be reflected in the real environment
              #   variable `TZ' before call of `tzset'.
              #
              # Note: unlike LANG, TZ doesn't seem to change behavior if it's
              # not exported.
              #
              # TODO: In Oil, provide an API that doesn't rely on libc's
              # global state.

              tzcell = self.mem.GetCell('TZ')
              if tzcell and tzcell.exported and tzcell.val.tag_() == value_e.Str:
                tzval = cast(value__Str, tzcell.val)
                posix.putenv('TZ', tzval.s)

              time_.tzset()

              # Handle special values:
              #   User can specify two special values -1 and -2 as in Bash
              #   Reference Manual 4.2: "Two special argument values may be
              #   used: -1 represents the current time, and -2 represents the
              #   time the shell was invoked." from
              #   https://www.gnu.org/software/bash/manual/html_node/Bash-Builtins.html#index-printf
              if d == -1: # the current time
                ts = time_.time()
              elif d == -2: # the shell start time
                ts = self.shell_start_time
              else:
                ts = d

              s = time_.strftime(typ[1:-2], time_.localtime(ts))
              if precision >= 0:
                s = s[:precision]  # truncate

            else:
              raise AssertionError()

          else:
            raise AssertionError()

          if width >= 0:
            if len(flags):
              if '-' in flags:
                s = s.ljust(width, ' ')
              elif '0' in flags:
                s = s.rjust(width, '0')
              else:
                pass
            else:
              s = s.rjust(width, ' ')

          out.append(s)

        else:
          raise AssertionError()

        if backslash_c:  # 'printf %b a\cb xx' - \c terminates processing!
          break

      if arg_index >= num_args:
        break
      # Otherwise there are more args.  So cycle through the loop once more to
      # implement the 'arg recycling' behavior.

    result = ''.join(out)
    if arg.v is not None:
      # TODO: get the span_id for arg.v!
      v_spid = runtime.NO_SPID

      arena = self.parse_ctx.arena
      a_parser = self.parse_ctx.MakeArithParser(arg.v)

      with alloc.ctx_Location(arena, source.ArgvWord(v_spid)):
        try:
          anode = a_parser.Parse()
        except error.Parse as e:
          ui.PrettyPrintError(e, arena)  # show parse error
          e_usage('Invalid -v expression', span_id=v_spid)

      lval = self.arith_ev.EvalArithLhs(anode, v_spid)

      if not self.exec_opts.eval_unsafe_arith() and lval.tag_() != lvalue_e.Named:
        e_usage('-v expected a variable name.  shopt -s eval_unsafe_arith allows expressions', span_id=v_spid)

      state.SetRef(self.mem, lval, value.Str(result))
    else:
      mylib.Stdout().write(result)
    return 0
