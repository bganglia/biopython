# Copyright 2017-2019 Damien Goutte-Gattat.  All rights reserved.
#
# This file is part of the Biopython distribution and governed by your
# choice of the "Biopython License Agreement" or the "BSD 3-Clause License".
# Please see the LICENSE file that should have been included as part of this
# package.

"""Bio.SeqIO support for the "xdna" file format.

The Xdna binary format is generated by Christian Marck's DNA Strider program
and also used by Serial Cloner.
"""

from re import match
from struct import pack, unpack
import warnings

from Bio import Alphabet, BiopythonWarning
from Bio._utils import _read_header
from Bio.Seq import Seq
from Bio.SeqIO.Interfaces import SequenceWriter
from Bio.SeqFeature import SeqFeature, FeatureLocation, ExactPosition
from Bio.SeqRecord import SeqRecord
from Bio.File import as_handle


_seq_types = {
    0: Alphabet.generic_alphabet,
    1: Alphabet.generic_dna,
    2: Alphabet.generic_dna,
    3: Alphabet.generic_rna,
    4: Alphabet.generic_protein,
}

_seq_topologies = {0: "linear", 1: "circular"}


def _read(handle, length):
    """Read the specified number of bytes from the given handle."""
    data = handle.read(length)
    if len(data) < length:
        raise ValueError("Cannot read %d bytes from handle" % length)
    return data


def _read_pstring(handle):
    """Read a Pascal string.

    A Pascal string comprises a single byte giving the length of the string
    followed by as many bytes.
    """
    length = unpack(">B", _read(handle, 1))[0]
    return unpack("%ds" % length, _read(handle, length))[0].decode("ASCII")


def _read_pstring_as_integer(handle):
    return int(_read_pstring(handle))


def _read_overhang(handle):
    """Read an overhang specification.

    An overhang is represented in a XDNA file as:
      - a Pascal string containing the text representation of the overhang
        length, which also indicates the nature of the overhang:
        - a length of zero means no overhang,
        - a negative length means a 3' overhang,
        - a positive length means a 5' overhang;
      - the actual overhang sequence.

    Examples:
      - 0x01 0x30: no overhang ("0", as a P-string)
      - 0x01 0x32 0x41 0x41: 5' AA overhang (P-string "2", then "AA")
      - 0x02 0x2D 0x31 0x43: 3' C overhang (P-string "-1", then "C")

    Returns a tuple (length, sequence).

    """
    length = _read_pstring_as_integer(handle)
    if length != 0:
        overhang = _read(handle, abs(length))
        return (length, overhang)
    else:
        return (None, None)


def _parse_feature_description(desc, qualifiers):
    """Parse the description field of a Xdna feature.

    The 'description' field of a feature sometimes contains several
    GenBank-like qualifiers, separated by carriage returns (CR, 0x0D).
    """
    # Split the field's value in CR-separated lines, skipping empty lines
    for line in [x for x in desc.split("\x0D") if len(x) > 0]:
        # Is it a qualifier="value" line?
        m = match('^([^=]+)="([^"]+)"?$', line)
        if m:
            # Store the qualifier as provided
            qual, value = m.groups()
            qualifiers[qual] = [value]
        elif '"' not in line:  # Reject ill-formed qualifiers
            # Store the entire line as a generic note qualifier
            qualifiers["note"] = [line]


def _read_feature(handle, record):
    """Read a single sequence feature."""
    name = _read_pstring(handle)
    desc = _read_pstring(handle)
    type = _read_pstring(handle) or "misc_feature"
    start = _read_pstring_as_integer(handle)
    end = _read_pstring_as_integer(handle)

    # Feature flags (4 bytes):
    # byte 1 is the strand (0: reverse strand, 1: forward strand);
    # byte 2 tells whether to display the feature;
    # byte 4 tells whether to draw an arrow when displaying the feature;
    # meaning of byte 3 is unknown.
    (forward, display, arrow) = unpack(">BBxB", _read(handle, 4))
    if forward:
        strand = 1
    else:
        strand = -1
        start, end = end, start

    # The last field is a Pascal string usually containing a
    # comma-separated triplet of numbers ranging from 0 to 255.
    # I suspect this represents the RGB color to use when displaying
    # the feature. Skip it as we have no need for it.
    _read_pstring(handle)

    # Assemble the feature
    # Shift start by -1 as XDNA feature coordinates are 1-based
    # while Biopython uses 0-based couting.
    location = FeatureLocation(start - 1, end, strand=strand)
    qualifiers = {}
    if name:
        qualifiers["label"] = [name]
    _parse_feature_description(desc, qualifiers)
    feature = SeqFeature(location, type=type, qualifiers=qualifiers)
    record.features.append(feature)


def XdnaIterator(handle):
    """Parse a Xdna file and return a SeqRecord object.

    Note that this is an "iterator" in name only since a Xdna file always
    contain a single sequence.
    """
    # Parse fixed-size header and do some rudimentary checks
    #
    # The "neg_length" value is the length of the part of the sequence
    # before the nucleotide considered as the "origin" (nucleotide number 1,
    # which in DNA Strider is not always the first nucleotide).
    # Biopython's SeqRecord has no such concept of a sequence origin as far
    # as I know, so we ignore that value. SerialCloner has no such concept
    # either and always generates files with a neg_length of zero.

    with as_handle(handle, "rb") as handle:

        header = _read_header(handle, 112)
        (version, type, topology, length, neg_length, com_length) = unpack(
            ">BBB25xII60xI12x", header
        )
        if version != 0:
            raise ValueError("Unsupported XDNA version")
        if type not in _seq_types:
            raise ValueError("Unknown sequence type")

        # Read actual sequence and comment found in all XDNA files
        sequence = _read(handle, length).decode("ASCII")
        comment = _read(handle, com_length).decode("ASCII")

        # Try to derive a name from the first "word" of the comment
        name = comment.split(" ")[0]

        # Create record object
        record = SeqRecord(
            Seq(sequence, _seq_types[type]), description=comment, name=name, id=name
        )
        if topology in _seq_topologies:
            record.annotations["topology"] = _seq_topologies[topology]

        if len(handle.read(1)) == 1:
            # This is an XDNA file with an optional annotation section.

            # Skip the overhangs as I don't know how to represent
            # them in the SeqRecord model.
            _read_overhang(handle)  # right-side overhang
            _read_overhang(handle)  # left-side overhang

            # Read the features
            num_features = unpack(">B", _read(handle, 1))[0]
            while num_features > 0:
                _read_feature(handle, record)
                num_features -= 1

        yield record


class XdnaWriter(SequenceWriter):
    """Write files in the Xdna format."""

    def write_file(self, records):
        """Write the specified record to a Xdna file.

        Note that the function expects a list of records as per the
        SequenceWriter interface, but the list should contain only one
        record as the Xdna format is a mono-record format.
        """
        if not records:
            raise ValueError("Must have one sequence")
        if len(records) > 1:
            raise ValueError("More than one sequence found")

        record = records[0]
        self._has_truncated_strings = False

        alptype = Alphabet._get_base_alphabet(record.seq.alphabet)
        if isinstance(alptype, Alphabet.DNAAlphabet):
            seqtype = 1
        elif isinstance(alptype, Alphabet.RNAAlphabet):
            seqtype = 3
        elif isinstance(alptype, Alphabet.ProteinAlphabet):
            seqtype = 4
        else:
            seqtype = 0

        if record.annotations.get("topology", "linear") == "circular":
            topology = 1
        else:
            topology = 0

        # We store the record's id and description in the comment field.
        # Make sure to avoid duplicating the id if it is already
        # contained in the description.
        if record.description.startswith(record.id):
            comment = record.description
        else:
            comment = f"{record.id} {record.description}"

        # Write header
        self.handle.write(
            pack(
                ">BBB25xII60xI11xB",
                0,  # version
                seqtype,
                topology,
                len(record),
                0,  # negative length
                len(comment),
                255,  # end of header
            )
        )

        # Actual sequence and comment
        self.handle.write(str(record.seq).encode("ASCII"))
        self.handle.write(comment.encode("ASCII"))

        self.handle.write(pack(">B", 0))  # Annotation section marker
        self._write_pstring("0")  # right-side overhang
        self._write_pstring("0")  # left-side overhand

        # Write features
        # We must skip features with fuzzy locations as they cannot be
        # represented in the Xdna format
        features = [
            f
            for f in record.features
            if type(f.location.start) == ExactPosition
            and type(f.location.end) == ExactPosition
        ]
        drop = len(record.features) - len(features)
        if drop > 0:
            warnings.warn(
                f"Dropping {drop} features with fuzzy locations", BiopythonWarning,
            )

        # We also cannot store more than 255 features as the number of
        # features is stored on a single byte...
        if len(features) > 255:
            drop = len(features) - 255
            warnings.warn(
                f"Too many features, dropping the last {drop}", BiopythonWarning
            )
            features = features[:255]

        self.handle.write(pack(">B", len(features)))
        for feature in features:
            self._write_pstring(feature.qualifiers.get("label", [""])[0])

            description = ""
            for qname in feature.qualifiers:
                if qname in ("label", "translation"):
                    continue

                for val in feature.qualifiers[qname]:
                    if len(description) > 0:
                        description = description + "\x0D"
                    description = description + '%s="%s"' % (qname, val)
            self._write_pstring(description)

            self._write_pstring(feature.type)

            start = feature.location.start.position + 1  # 1-based coordinates
            end = feature.location.end.position
            strand = 1
            if feature.location.strand == -1:
                start, end = end, start
                strand = 0
            self._write_pstring(str(start))
            self._write_pstring(str(end))

            self.handle.write(pack(">BBBB", strand, 1, 0, 1))
            self._write_pstring("127,127,127")

        if self._has_truncated_strings:
            warnings.warn(
                "Some annotations were truncated to 255 characters", BiopythonWarning
            )

        return 1

    def _write_pstring(self, s):
        """Write the given string as a Pascal string."""
        if len(s) > 255:
            self._has_truncated_strings = True
            s = s[:255]
        self.handle.write(pack(">B", len(s)))
        self.handle.write(s.encode("ASCII"))
