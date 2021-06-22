# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""An extensible ASCII table reader and writer.

cds.py:
  Classes to read CDS / Vizier table format

:Copyright: Smithsonian Astrophysical Observatory (2011)
:Author: Tom Aldcroft (aldcroft@head.cfa.harvard.edu), \
         Suyog Garg (suyog7130@gmail.com)
"""


import fnmatch
import itertools
import re
import os
from contextlib import suppress

from . import core
from . import fixedwidth

from astropy.units import Unit

from .CDSColumn import CDSColumn
import sys
from string import Template
from textwrap import wrap, fill
import math

MAX_SIZE_README_LINE = 80
MAX_COL_INTLIMIT = 10000000


__doctest_skip__ = ['*']

cdsdicts = {'title': 'Title ?',
            'author': '1st author ?',
            'catalogue': '',
            'date': 'Date ?',
            'abstract': 'Abstract ?',
            'authors': 'Authors ?',
            'bibcode': 'ref ?',
            'keywords': ''
            }

ByteByByteTemplate = ["Byte-by-byte Description of file: $file",
"--------------------------------------------------------------------------------",
" Bytes Format Units  Label     Explanations",
"--------------------------------------------------------------------------------",
"$bytebybyte",
"--------------------------------------------------------------------------------"]


class CdsSplitter(fixedwidth.FixedWidthSplitter):
    """
    Contains the join function to left align the CDS columns
    when writing to a file.
    """

    def join(self, vals, widths):
        pad = self.delimiter_pad or ''
        delimiter = self.delimiter or ''
        padded_delim = pad + delimiter + pad
        bookend_left = ''
        bookend_right = ''
        vals = [val + ' ' * (width - len(val)) for val, width in zip(vals, widths)]
        return bookend_left + padded_delim.join(vals) + bookend_right


class CdsHeader(core.BaseHeader):
    col_type_map = {'e': core.FloatType,
                    'f': core.FloatType,
                    'i': core.IntType,
                    'a': core.StrType}

    'The ReadMe file to construct header from.'
    readme = None

    def get_type_map_key(self, col):
        match = re.match(r'\d*(\S)', col.raw_type.lower())
        if not match:
            raise ValueError('Unrecognized CDS format "{}" for column "{}"'.format(
                col.raw_type, col.name))
        return match.group(1)

    def get_cols(self, lines):
        """
        Initialize the header Column objects from the table ``lines`` for a CDS
        header.

        Parameters
        ----------
        lines : list
            List of table lines

        """

        # Read header block for the table ``self.data.table_name`` from the read
        # me file ``self.readme``.
        if self.readme and self.data.table_name:
            in_header = False
            readme_inputter = core.BaseInputter()
            f = readme_inputter.get_lines(self.readme)
            # Header info is not in data lines but in a separate file.
            lines = []
            comment_lines = 0
            for line in f:
                line = line.strip()
                if in_header:
                    lines.append(line)
                    if line.startswith(('------', '=======')):
                        comment_lines += 1
                        if comment_lines == 3:
                            break
                else:
                    match = re.match(r'Byte-by-byte Description of file: (?P<name>.+)$',
                                     line, re.IGNORECASE)
                    if match:
                        # Split 'name' in case in contains multiple files
                        names = [s for s in re.split('[, ]+', match.group('name'))
                                 if s]
                        # Iterate on names to find if one matches the tablename
                        # including wildcards.
                        for pattern in names:
                            if fnmatch.fnmatch(self.data.table_name, pattern):
                                in_header = True
                                lines.append(line)
                                break

            else:
                raise core.InconsistentTableError("Can't find table {} in {}".format(
                    self.data.table_name, self.readme))

        found_line = False

        for i_col_def, line in enumerate(lines):
            if re.match(r'Byte-by-byte Description', line, re.IGNORECASE):
                found_line = True
            elif found_line:  # First line after list of file descriptions
                i_col_def -= 1  # Set i_col_def to last description line
                break

        re_col_def = re.compile(r"""\s*
                                    (?P<start> \d+ \s* -)? \s*
                                    (?P<end>   \d+)        \s+
                                    (?P<format> [\w.]+)     \s+
                                    (?P<units> \S+)        \s+
                                    (?P<name>  \S+)
                                    (\s+ (?P<descr> \S.*))?""",
                                re.VERBOSE)

        cols = []
        for line in itertools.islice(lines, i_col_def + 4, None):
            if line.startswith(('------', '=======')):
                break
            match = re_col_def.match(line)
            if match:
                col = core.Column(name=match.group('name'))
                col.start = int(re.sub(r'[-\s]', '',
                                       match.group('start') or match.group('end'))) - 1
                col.end = int(match.group('end'))
                unit = match.group('units')
                if unit == '---':
                    col.unit = None  # "---" is the marker for no unit in CDS table
                else:
                    col.unit = Unit(unit, format='cds', parse_strict='warn')
                col.description = (match.group('descr') or '').strip()
                col.raw_type = match.group('format')
                col.type = self.get_col_type(col)

                match = re.match(
                    r'(?P<limits>[\[\]] \S* [\[\]])?'  # Matches limits specifier (eg [])
                                                       # that may or may not be present
                    r'\?'  # Matches '?' directly
                    r'((?P<equal>=)(?P<nullval> \S*))?'  # Matches to nullval if and only
                                                         # if '=' is present
                    r'(?P<order>[-+]?[=]?)'  # Matches to order specifier:
                                             # ('+', '-', '+=', '-=')
                    r'(\s* (?P<descriptiontext> \S.*))?',  # Matches description text even
                                                           # even if no whitespace is
                                                           # present after '?'
                    col.description, re.VERBOSE)
                if match:
                    col.description = (match.group('descriptiontext') or '').strip()
                    if issubclass(col.type, core.FloatType):
                        fillval = 'nan'
                    else:
                        fillval = '0'

                    if match.group('nullval') == '-':
                        col.null = '---'
                        # CDS tables can use -, --, ---, or ---- to mark missing values
                        # see https://github.com/astropy/astropy/issues/1335
                        for i in [1, 2, 3, 4]:
                            self.data.fill_values.append(('-' * i, fillval, col.name))
                    else:
                        col.null = match.group('nullval')
                        if (col.null is None):
                            col.null = ''
                        self.data.fill_values.append((col.null, fillval, col.name))

                cols.append(col)
            else:  # could be a continuation of the previous col's description
                if cols:
                    cols[-1].description += line.strip()
                else:
                    raise ValueError(f'Line "{line}" not parsable as CDS header')

        self.names = [x.name for x in cols]

        self.cols = cols

    def init_CDSColumns(self):
        """Initialize list of CDSColumns  (self.__cds_columns)"""
        self.__cds_columns = []
        for col in self.cols:
            cdsCol = CDSColumn(col)
            self.__cds_columns.append(cdsCol)

    def __strFmt(self, string):
        """Return argument formatted as string."""
        if string is None:
            return ""
        else:
            return string

    def writeByteByByte(self):
        """
        Writes byte-by-byte description of the table.
        :param table: `astropy.table.Table` object.
        :param outBuffer: true to get buffer, else write on output (default: False)
        """
        # get column widths.
        vals_list = []
        col_str_iters = self.data.str_vals()
        for vals in zip(*col_str_iters):
            vals_list.append(vals)

        for i, col in enumerate(self.cols):
            col.width = max([len(vals[i]) for vals in vals_list])
            if self.start_line is not None:
                col.width = max(col.width, len(col.info.name))
        widths = [col.width for col in self.cols]

        self.init_CDSColumns()  # get CDSColumn objects.

        columns = self.__cds_columns
        startb = 1
        sz = [0, 0, 1, 7]
        l = len(str(sum(widths)))
        if l > sz[0]:
            sz[0] = l
            sz[1] = l
        fmtb = "{0:" + str(sz[0]) + "d}-{1:" + str(sz[1]) + "d} {2:" + str(sz[2]) + "s}"
        for column in columns:
            if len(column.name) > sz[3]:
                sz[3] = len(column.name)
        fmtb += " {3:6s} {4:6s} {5:" + str(sz[3]) + "s} {6:s}"
        buff = ""
        nsplit = sz[0] + sz[1] + sz[2] + sz[3] + 16

        for i, column in enumerate(columns):
            column.parse()  # set CDSColumn type, size and format.

            endb = column.size + startb - 1
            """ if column.formatter.fortran_format[0] == 'R':
                buff += self.__strFmtRa(column, fmtb, startb) + "\n"
            elif column.formatter.fortran_format[0] == 'D':
                buff += self.__strFmtDe(column, fmtb, startb) + "\n"
            else: """
            description = column.description
            if column.hasNull:
                nullflag = "?"
            else:
                nullflag = ""

            # add col limit values to col description
            borne = ""
            if column.min and column.max:
                if column.formatter.fortran_format[0] == 'I':
                    if abs(column.min) < MAX_COL_INTLIMIT and abs(column.max) < MAX_COL_INTLIMIT:
                        if column.min == column.max:
                            borne = "[{0}]".format(column.min)
                        else:
                            borne = "[{0}/{1}]".format(column.min, column.max)
                elif column.formatter.fortran_format[0] in ('E','F'):
                    borne = "[{0}/{1}]".format(math.floor(column.min*100)/100.,
                                                math.ceil(column.max*100)/100.)

            description = "{0}{1} {2}".format(borne, nullflag, description)
            newline = fmtb.format(startb, endb, "",
                                    self.__strFmt(column.formatter.fortran_format),
                                    self.__strFmt(column.unit),
                                    self.__strFmt(column.name),
                                    description)

            if len(newline) > MAX_SIZE_README_LINE:
                buff += ("\n").join(wrap(newline,
                                            subsequent_indent=" " * nsplit,
                                            width=MAX_SIZE_README_LINE))
                buff += "\n"
            else:
                buff += newline + "\n"
            startb = endb + 2

        notes = self.cdsdicts.get('notes', None)
        if notes is not None:
            buff += "-" * 80 + "\n"
            for line in notes:
                buff += line + "\n"
            buff += "-" * 80 + "\n"

        return buff

    def write(self, lines):
        bbb = Template('\n'.join(ByteByByteTemplate))
        ByteByByte = bbb.substitute({'file': 'table.dat',
                                     'bytebybyte': self.writeByteByByte()})
        lines.append(ByteByByte)


class CdsData(fixedwidth.FixedWidthData):
    """CDS table data reader
    """
    splitter_class = CdsSplitter

    def process_lines(self, lines):
        """Skip over CDS header by finding the last section delimiter"""
        # If the header has a ReadMe and data has a filename
        # then no need to skip, as the data lines do not have header
        # info. The ``read`` method adds the table_name to the ``data``
        # attribute.
        if self.header.readme and self.table_name:
            return lines
        i_sections = [i for i, x in enumerate(lines)
                      if x.startswith(('------', '======='))]
        if not i_sections:
            raise core.InconsistentTableError('No CDS section delimiter found')
        return lines[i_sections[-1]+1:]  # noqa

    def write(self, lines):
        self.splitter.delimiter = ' '
        fixedwidth.FixedWidthData.write(self, lines)


class Cds(core.BaseReader):
    """CDS format table.

    See: http://vizier.u-strasbg.fr/doc/catstd.htx

    Example::

      Table: Table name here
      = ==============================================================================
      Catalog reference paper
          Bibliography info here
      ================================================================================
      ADC_Keywords: Keyword ; Another keyword ; etc

      Description:
          Catalog description here.
      ================================================================================
      Byte-by-byte Description of file: datafile3.txt
      --------------------------------------------------------------------------------
         Bytes Format Units  Label  Explanations
      --------------------------------------------------------------------------------
         1-  3 I3     ---    Index  Running identification number
         5-  6 I2     h      RAh    Hour of Right Ascension (J2000)
         8-  9 I2     min    RAm    Minute of Right Ascension (J2000)
        11- 15 F5.2   s      RAs    Second of Right Ascension (J2000)
      --------------------------------------------------------------------------------
      Note (1): A CDS file can contain sections with various metadata.
                Notes can be multiple lines.
      Note (2): Another note.
      --------------------------------------------------------------------------------
        1 03 28 39.09
        2 04 18 24.11

    **About parsing the CDS format**

    The CDS format consists of a table description and the table data.  These
    can be in separate files as a ``ReadMe`` file plus data file(s), or
    combined in a single file.  Different subsections within the description
    are separated by lines of dashes or equal signs ("------" or "======").
    The table which specifies the column information must be preceded by a line
    starting with "Byte-by-byte Description of file:".

    In the case where the table description is combined with the data values,
    the data must be in the last section and must be preceded by a section
    delimiter line (dashes or equal signs only).

    **Basic usage**

    Use the ``ascii.read()`` function as normal, with an optional ``readme``
    parameter indicating the CDS ReadMe file.  If not supplied it is assumed that
    the header information is at the top of the given table.  Examples::

      >>> from astropy.io import ascii
      >>> table = ascii.read("data/cds.dat")
      >>> table = ascii.read("data/vizier/table1.dat", readme="data/vizier/ReadMe")
      >>> table = ascii.read("data/cds/multi/lhs2065.dat", readme="data/cds/multi/ReadMe")
      >>> table = ascii.read("data/cds/glob/lmxbrefs.dat", readme="data/cds/glob/ReadMe")

    The table name and the CDS ReadMe file can be entered as URLs.  This can be used
    to directly load tables from the Internet.  For example, Vizier tables from the
    CDS::

      >>> table = ascii.read("ftp://cdsarc.u-strasbg.fr/pub/cats/VII/253/snrs.dat",
      ...             readme="ftp://cdsarc.u-strasbg.fr/pub/cats/VII/253/ReadMe")

    If the header (ReadMe) and data are stored in a single file and there
    is content between the header and the data (for instance Notes), then the
    parsing process may fail.  In this case you can instruct the reader to
    guess the actual start of the data by supplying ``data_start='guess'`` in the
    call to the ``ascii.read()`` function.  You should verify that the output
    data table matches expectation based on the input CDS file.

    **Using a reader object**

    When ``Cds`` reader object is created with a ``readme`` parameter
    passed to it at initialization, then when the ``read`` method is
    executed with a table filename, the header information for the
    specified table is taken from the ``readme`` file.  An
    ``InconsistentTableError`` is raised if the ``readme`` file does not
    have header information for the given table.

      >>> readme = "data/vizier/ReadMe"
      >>> r = ascii.get_reader(ascii.Cds, readme=readme)
      >>> table = r.read("data/vizier/table1.dat")
      >>> # table5.dat has the same ReadMe file
      >>> table = r.read("data/vizier/table5.dat")

    If no ``readme`` parameter is specified, then the header
    information is assumed to be at the top of the given table.

      >>> r = ascii.get_reader(ascii.Cds)
      >>> table = r.read("data/cds.dat")
      >>> #The following gives InconsistentTableError, since no
      >>> #readme file was given and table1.dat does not have a header.
      >>> table = r.read("data/vizier/table1.dat")
      Traceback (most recent call last):
        ...
      InconsistentTableError: No CDS section delimiter found

    Caveats:

    * The Units and Explanations are available in the column ``unit`` and
      ``description`` attributes, respectively.
    * The other metadata defined by this format is not available in the output table.
    """
    _format_name = 'cds'
    _io_registry_format_aliases = ['cds']
    #_io_registry_can_write = False
    _description = 'CDS format table'

    data_class = CdsData
    header_class = CdsHeader

    def __init__(self, readme=None):
        super().__init__()
        self.header.readme = readme
        self.cdsdicts = cdsdicts
        self.header.cdsdicts = self.cdsdicts
        self.data.cdsdicts = self.cdsdicts

    def write(self, table=None):
        self.data.header = self.header
        self.header.position_line = None
        self.header.start_line = None
        self.data.start_line = None
        return core.BaseReader.write(self, table=table)

    def read(self, table):
        # If the read kwarg `data_start` is 'guess' then the table may have extraneous
        # lines between the end of the header and the beginning of data.
        if self.data.start_line == 'guess':
            # Replicate the first part of BaseReader.read up to the point where
            # the table lines are initially read in.
            with suppress(TypeError):
                # For strings only
                if os.linesep not in table + '':
                    self.data.table_name = os.path.basename(table)

            self.data.header = self.header
            self.header.data = self.data

            # Get a list of the lines (rows) in the table
            lines = self.inputter.get_lines(table)

            # Now try increasing data.start_line by one until the table reads successfully.
            # For efficiency use the in-memory list of lines instead of `table`, which
            # could be a file.
            for data_start in range(len(lines)):
                self.data.start_line = data_start
                with suppress(Exception):
                    table = super().read(lines)
                    return table
        else:
            return super().read(table)