#!/usr/bin/python

#Audio Tools, a module and set of tools for manipulating audio data
#Copyright (C) 2007-2012  Brian Langenberger

#This program is free software; you can redistribute it and/or modify
#it under the terms of the GNU General Public License as published by
#the Free Software Foundation; either version 2 of the License, or
#(at your option) any later version.

#This program is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.

#You should have received a copy of the GNU General Public License
#along with this program; if not, write to the Free Software
#Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

from audiotools import (AudioFile, InvalidFile, PCMReader,
                        __capped_stream_reader__, PCMReaderError,
                        transfer_data, DecodingError, EncodingError,
                        ID3v22Comment, BUFFER_SIZE, ChannelMask,
                        ReorderedPCMReader, pcm,
                        cStringIO, os, AiffContainer, to_pcm_progress,
                        LimitedFileReader)
import struct
import gettext

gettext.install("audiotools", unicode=True)


def parse_ieee_extended(bitstream):
    (signed, exponent, mantissa) = bitstream.parse("1u 15u 64U")
    if ((exponent == 0) and (mantissa == 0)):
        return 0
    elif (exponent == 0x7FFF):
        return 1.79769313486231e+308
    else:
        f = mantissa * (2.0 ** (exponent - 16383 - 63))
        return f if not signed else -f


def build_ieee_extended(bitstream, value):
    from math import frexp

    if (value < 0):
        signed = 1
        value = abs(value)
    else:
        signed = 0

    (fmant, exponent) = frexp(value)
    if ((exponent > 16384) or (fmant >= 1)):
        exponent = 0x7FFF
        mantissa = 0
    else:
        exponent += 16382
        mantissa = fmant * (2 ** 64)

    bitstream.build("1u 15u 64U", (signed, exponent, mantissa))

#######################
#AIFF
#######################


class AIFF_Chunk:
    """a raw chunk of AIFF data"""

    def __init__(self, chunk_id, chunk_size, chunk_data):
        """chunk_id should be a binary string of ASCII
        chunk_size is the length of chunk_data
        chunk_data should be a binary string of chunk data"""

        #FIXME - check chunk_id's validity

        self.id = chunk_id
        self.__size__ = chunk_size
        self.__data__ = chunk_data

    def __repr__(self):
        return "AIFF_Chunk(%s)" % (repr(self.id))

    def size(self):
        """returns size of chunk in bytes
        not including any spacer byte for odd-sized chunks"""

        return self.__size__

    def total_size(self):
        """returns the total size of the chunk
        including the 8 byte ID/size and any padding byte"""

        if (self.__size__ % 2):
            return 8 + self.__size__ + 1
        else:
            return 8 + self.__size__

    def data(self):
        """returns chunk data as file-like object"""

        return cStringIO.StringIO(self.__data__)

    def verify(self):
        return self.__size__ == len(self.__data__)

    def write(self, f):
        """writes the entire chunk to the given output file object
        returns size of entire chunk (including header and spacer)
        in bytes"""

        f.write(self.id)
        f.write(struct.pack(">I", self.__size__))
        f.write(self.__data__)
        if (self.__size__ % 2):
            f.write(chr(0))
        return self.total_size()


class AIFF_File_Chunk(AIFF_Chunk):
    """a raw chunk of AIFF data taken from an existing file"""

    def __init__(self, chunk_id, chunk_size, aiff_file, chunk_data_offset):
        """chunk_id should be a binary string of ASCII
        chunk_size is the size of the chunk in bytes
        (not counting any spacer byte)
        aiff_file is the file this chunk belongs to
        chunk_data_offset is the offset to the chunk's data bytes
        (not including the 8 byte header)"""

        self.id = chunk_id
        self.__size__ = chunk_size
        self.__aiff_file__ = aiff_file
        self.__offset__ = chunk_data_offset

    def __repr__(self):
        return "AIFF_File_Chunk(%s)" % (repr(self.id))

    def data(self):
        """returns chunk data as file-like object"""

        self.__wav_file__.seek(self.__offset__)
        return LimitedFileReader(self.__wav_file__, self.size())

    def verify(self):
        self.__aiff_file__.seek(self.__offset__)
        to_read = self.__size__
        while (to_read > 0):
            s = self.__aiff_file__.read(min(0x100000, to_read))
            if (len(s) == 0):
                return False
            else:
                to_read -= len(s)
        return True

    def write(self, f):
        """writes the entire chunk to the given output file object
        returns size of entire chunk (including header and spacer)
        in bytes"""

        f.write(self.id)
        f.write(struct.pack(">I", self.__size__))
        self.__aiff_file__.seek(self.__offset__)
        to_write = self.__size__
        while (to_write > 0):
            s = self.__aiff_file__.read(min(0x100000, to_write))
            f.write(s)
            to_write -= len(s)

        if (self.__size__ % 2):
            f.write(chr(0))
        return self.total_size()


def parse_comm(comm):
    """given a COMM chunk (without the 8 byte name/size header)
    returns (channels, total_sample_frames, bits_per_sample,
             sample_rate, channel_mask)
    where channel_mask is a ChannelMask object and the rest are ints
    may raise IOError if an error occurs reading the chunk"""

    (channels,
     total_sample_frames,
     bits_per_sample) = comm.parse("16u 32u 16u")
    sample_rate = int(parse_ieee_extended(comm))

    if (channels <= 2):
        channel_mask = ChannelMask.from_channels(channels)
    else:
        channel_mask = ChannelMask(0)

    return (channels, total_sample_frames, bits_per_sample,
            sample_rate, channel_mask)


class AiffReader(PCMReader):
    """A subclass of PCMReader for reading AIFF file contents."""

    def __init__(self, aiff_file,
                 sample_rate, channels, channel_mask, bits_per_sample,
                 total_frames, process=None):
        """aiff_file should be a file-like object of aiff data

        sample_rate, channels, channel_mask and bits_per_sample are ints."""

        self.file = aiff_file
        self.sample_rate = sample_rate
        self.channels = channels
        self.channel_mask = channel_mask
        self.bits_per_sample = bits_per_sample
        self.remaining_frames = total_frames
        self.bytes_per_frame = self.channels * self.bits_per_sample / 8

        self.process = process

        from .bitstream import BitstreamReader

        #build a capped reader for the ssnd chunk
        aiff_reader = BitstreamReader(aiff_file, 0)
        try:
            (form, aiff) = aiff_reader.parse("4b 32p 4b")
            if (form != 'FORM'):
                raise InvalidAIFF(_(u"Not an AIFF file"))
            elif (aiff != 'AIFF'):
                raise InvalidAIFF(_(u"Invalid AIFF file"))

            while (True):
                (chunk_id, chunk_size) = aiff_reader.parse("4b 32u")
                if (chunk_id == 'SSND'):
                    #adjust for the SSND alignment
                    aiff_reader.skip(64)
                    break
                else:
                    aiff_reader.skip_bytes(chunk_size)
                    if (chunk_size % 2):
                        aiff_reader.skip(8)
        except IOError:
            self.read = self.read_error

    def read(self, bytes):
        """Try to read a pcm.FrameList of size "bytes"."""

        #convert bytes to a number of PCM frames
        frames_read = min(max(bytes / self.bytes_per_frame, 1),
                          self.remaining_frames)

        if (frames_read > 0):
            pcm_data = self.file.read(frames_read * self.bytes_per_frame)
            if (len(pcm_data) < frames_read * self.bytes_per_frame):
                raise IOError("ssnd chunk ends prematurely")
            else:
                framelist = pcm.FrameList(pcm_data,
                                          self.channels,
                                          self.bits_per_sample,
                                          True, True)
                self.remaining_frames -= framelist.frames
                return framelist

        else:
            return pcm.FrameList("",
                                 self.channels,
                                 self.bits_per_sample,
                                 True, True)

    def read_error(self, bytes):
        raise IOError()


class InvalidAIFF(InvalidFile):
    """Raised if some problem occurs parsing AIFF chunks."""

    pass


class AiffAudio(AiffContainer):
    """An AIFF audio file."""

    SUFFIX = "aiff"
    NAME = SUFFIX

    PRINTABLE_ASCII = frozenset([chr(i) for i in xrange(0x20, 0x7E + 1)])

    def __init__(self, filename):
        """filename is a plain string."""

        self.filename = filename

        self.__channels__ = 0
        self.__bits_per_sample__ = 0
        self.__sample_rate__ = 0
        self.__channel_mask__ = ChannelMask(0)
        self.__total_sample_frames__ = 0

        from .bitstream import BitstreamReader

        try:
            for chunk in self.chunks():
                if (chunk.id == "COMM"):
                    try:
                        (self.__channels__,
                         self.__total_sample_frames__,
                         self.__bits_per_sample__,
                         self.__sample_rate__,
                         self.__channel_mask__) = parse_comm(
                            BitstreamReader(chunk.data(), 0))
                        break
                    except IOError:
                        continue
        except IOError:
            raise InvalidAIFF("I/O error reading wave")

    def bits_per_sample(self):
        """Returns an integer number of bits-per-sample this track contains."""

        return self.__bits_per_sample__

    def channels(self):
        """Returns an integer number of channels this track contains."""

        return self.__channels__

    def channel_mask(self):
        """Returns a ChannelMask object of this track's channel layout."""

        return self.__channel_mask__

    def lossless(self):
        """Returns True."""

        return True

    def total_frames(self):
        """Returns the total PCM frames of the track as an integer."""

        return self.__total_sample_frames__

    def sample_rate(self):
        """Returns the rate of the track's audio as an integer number of Hz."""

        return self.__sample_rate__

    @classmethod
    def is_type(cls, file):
        """Returns True if the given file object describes this format.

        Takes a seekable file pointer rewound to the start of the file."""

        header = file.read(12)

        return ((header[0:4] == 'FORM') and
                (header[8:12] == 'AIFF'))

    def chunks(self):
        """yields a (chunk_id, chunk_size, chunk_data) tuples

        all fields are binary strings"""

        aiff_file = file(self.filename, 'rb')
        try:
            (form,
             total_size,
             aiff) = struct.unpack(">4sI4s", aiff_file.read(12))
        except struct.error:
            raise InvalidAIFF(_(u"Invalid AIFF file"))

        if (form != 'FORM'):
            raise InvalidAIFF(_(u"Not an AIFF file"))
        elif (aiff != 'AIFF'):
            raise InvalidAIFF(_(u"Invalid AIFF file"))
        else:
            total_size -= 4

        while (total_size > 0):
            #read the chunk header and ensure its validity
            try:
                data = aiff_file.read(8)
                (chunk_id,
                 chunk_size) = struct.unpack(">4sI", data)
            except struct.error:
                raise InvalidAIFF(_(u"Invalid AIFF file"))

            if (not frozenset(chunk_id).issubset(self.PRINTABLE_ASCII)):
                raise InvalidAIFF(_(u"Invalid AIFF chunk ID"))
            else:
                total_size -= 8

            #yield RIFF_Chunk or RIFF_File_Chunk depending on chunk size
            if (chunk_size >= 0x100000):
                #if chunk is too large, yield a File_Chunk
                yield AIFF_File_Chunk(chunk_id,
                                      chunk_size,
                                      file(self.filename, "rb"),
                                      aiff_file.tell())
                aiff_file.seek(chunk_size, 1)
            else:
                #otherwise, yield a raw data Chunk
                yield AIFF_Chunk(chunk_id, chunk_size,
                                 aiff_file.read(chunk_size))

            if (chunk_size % 2):
                if (len(aiff_file.read(1)) < 1):
                    raise InvalidAIFF(_(u"Invalid AIFF chunk"))
                total_size -= (chunk_size + 1)
            else:
                total_size -= chunk_size

    @classmethod
    def aiff_from_chunks(cls, filename, chunk_iter):
        """Builds a new AIFF file from a chunk data iterator.

        filename is the path to the AIFF file to build.
        chunk_iter should yield AIFF_Chunk-compatible objects.
        """

        aiff_file = file(filename, 'wb')
        try:
            total_size = 4

            #write an unfinished header with a placeholder size
            aiff_file.write(struct.pack(">4sI4s", "FORM", total_size, "AIFF"))

            #write the individual chunks
            for chunk in chunk_iter:
                total_size += chunk.write(aiff_file)

            #once the chunks are done, go back and re-write the header
            aiff_file.seek(0, 0)
            aiff_file.write(struct.pack(">4sI4s", "FORM", total_size, "AIFF"))
        finally:
            aiff_file.close()

    def get_metadata(self):
        """Returns a MetaData object, or None.

        Raises IOError if unable to read the file."""

        from .bitstream import BitstreamReader

        for chunk in self.chunks():
            if (chunk.id == 'ID3 '):
                return ID3v22Comment.parse(BitstreamReader(chunk.data(), 0))
        else:
            return None

    def update_metadata(self, metadata):
        """Takes this track's current MetaData object
        as returned by get_metadata() and sets this track's metadata
        with any fields updated in that object.

        Raises IOError if unable to write the file.
        """

        import tempfile
        from .bitstream import BitstreamRecorder

        if (metadata is None):
            return
        elif (not isinstance(metadata, ID3v22Comment)):
            raise _(u"metadata not from audio file")

        def chunk_filter(chunks, id3_chunk_data):
            for chunk in chunks:
                if (chunk.id == "ID3 "):
                    yield AIFF_Chunk("ID3 ",
                                     len(id3_chunk_data),
                                     id3_chunk_data)
                else:
                    yield chunk

        #turn our ID3v2.2 tag into a raw binary chunk
        id3_chunk = BitstreamRecorder(0)
        metadata.build(id3_chunk)
        id3_chunk = id3_chunk.data()

        #generate a temporary AIFF file in which our new ID3v2.2 chunk
        #replaces the existing ID3v2.2 chunk
        new_aiff = tempfile.NamedTemporaryFile(suffix=self.SUFFIX)
        self.__class__.aiff_from_chunks(new_aiff.name,
                                        chunk_filter(self.chunks(),
                                                     id3_chunk))

        #replace the existing file with data from the temporary file
        new_file = open(new_aiff.name, 'rb')
        old_file = open(self.filename, 'wb')
        transfer_data(new_file.read, old_file.write)
        old_file.close()
        new_file.close()

    def set_metadata(self, metadata):
        """Takes a MetaData object and sets this track's metadata.

        This metadata includes track name, album name, and so on.
        Raises IOError if unable to write the file."""

        if (metadata is None):
            return
        elif (self.get_metadata() is not None):
            #current file has metadata, so replace it with new metadata
            self.update_metadata(ID3v22Comment.converted(metadata))
        else:
            #current file has no metadata, so append new ID3 block

            import tempfile
            from .bitstream import BitstreamRecorder

            def chunk_filter(chunks, id3_chunk_data):
                for chunk in chunks:
                    yield chunk

                yield AIFF_Chunk("ID3 ",
                                 len(id3_chunk_data),
                                 id3_chunk_data)

            #turn our ID3v2.2 tag into a raw binary chunk
            id3_chunk = BitstreamRecorder(0)
            ID3v22Comment.converted(metadata).build(id3_chunk)
            id3_chunk = id3_chunk.data()

            #generate a temporary AIFF file in which our new ID3v2.2 chunk
            #is appended to the file's set of chunks
            new_aiff = tempfile.NamedTemporaryFile(suffix=self.SUFFIX)
            self.__class__.aiff_from_chunks(new_aiff.name,
                                            chunk_filter(self.chunks(),
                                                         id3_chunk))

            #replace the existing file with data from the temporary file
            new_file = open(new_aiff.name, 'rb')
            old_file = open(self.filename, 'wb')
            transfer_data(new_file.read, old_file.write)
            old_file.close()
            new_file.close()

    def delete_metadata(self):
        """Deletes the track's MetaData.

        This removes or unsets tags as necessary in order to remove all data.
        Raises IOError if unable to write the file."""

        def chunk_filter(chunks):
            for chunk in chunks:
                if (chunk.id == 'ID3 '):
                    continue
                else:
                    yield chunk

        import tempfile

        new_aiff = tempfile.NamedTemporaryFile(suffix=self.SUFFIX)
        self.__class__.aiff_from_chunks(new_aiff.name,
                                        chunk_filter(self.chunks()))

        new_file = open(new_aiff.name, 'rb')
        old_file = open(self.filename, 'wb')
        transfer_data(new_file.read, old_file.write)
        old_file.close()
        new_file.close()

    def to_pcm(self):
        """Returns a PCMReader object containing the track's PCM data."""

        return AiffReader(file(self.filename, 'rb'),
                          sample_rate=self.sample_rate(),
                          channels=self.channels(),
                          bits_per_sample=self.bits_per_sample(),
                          channel_mask=int(self.channel_mask()),
                          total_frames=self.__total_sample_frames__)

    @classmethod
    def from_pcm(cls, filename, pcmreader, compression=None):
        """Encodes a new file from PCM data.

        Takes a filename string, PCMReader object
        and optional compression level string.
        Encodes a new audio file from pcmreader's data
        at the given filename with the specified compression level
        and returns a new AiffAudio object."""

        from .bitstream import BitstreamWriter

        try:
            f = open(filename, 'wb')
            aiff = BitstreamWriter(f, 0)
        except IOError, msg:
            raise EncodingError(str(msg))

        try:
            total_size = 0
            data_size = 0
            total_pcm_frames = 0

            #write out the basic headers first
            #we'll be back later to clean up the sizes
            aiff.build("4b 32u 4b", ("FORM", total_size, "AIFF"))
            total_size += 4

            aiff.build("4b 32u", ("COMM", 0x12))
            total_size += 8

            aiff.build("16u 32u 16u", (pcmreader.channels,
                                       total_pcm_frames,
                                       pcmreader.bits_per_sample))
            build_ieee_extended(aiff, pcmreader.sample_rate)
            total_size += 0x12

            aiff.build("4b 32u", ("SSND", data_size))
            total_size += 8

            aiff.build("32u 32u", (0, 0))
            data_size += 8
            total_size += 8

            #dump pcmreader's FrameLists into the file as big-endian
            try:
                framelist = pcmreader.read(BUFFER_SIZE)
                while (len(framelist) > 0):
                    bytes = framelist.to_bytes(True, True)
                    f.write(bytes)

                    total_size += len(bytes)
                    data_size += len(bytes)
                    total_pcm_frames += framelist.frames

                    framelist = pcmreader.read(BUFFER_SIZE)
            except (IOError, ValueError), err:
                cls.__unlink__(filename)
                raise EncodingError(str(err))
            except Exception, err:
                cls.__unlink__(filename)
                raise err

            #handle odd-sized data chunks
            if (data_size % 2):
                aiff.write(8, 0)
                total_size += 1

            #close the PCM reader and flush our output
            try:
                pcmreader.close()
            except DecodingError, err:
                cls.__unlink__(filename)
                raise EncodingError(err.error_message)
            f.flush()

            if (total_size < (2 ** 32)):
                #go back to the beginning and rewrite the header
                f.seek(0, 0)
                aiff.build("4b 32u 4b", ("FORM", total_size, "AIFF"))
                aiff.build("4b 32u", ("COMM", 0x12))
                aiff.build("16u 32u 16u", (pcmreader.channels,
                                           total_pcm_frames,
                                           pcmreader.bits_per_sample))
                build_ieee_extended(aiff, pcmreader.sample_rate)
                aiff.build("4b 32u", ("SSND", data_size))
            else:
                os.unlink(filename)
                raise EncodingError("PCM data too large for aiff file")

        finally:
            f.close()

        return AiffAudio(filename)

    def to_aiff(self, aiff_filename, progress=None):
        """Writes the contents of this file to the given .aiff filename string.

        Raises EncodingError if some error occurs during decoding."""

        try:
            self.verify()
        except InvalidAIFF, err:
            raise EncodingError(str(err))

        try:
            output = file(aiff_filename, 'wb')
            input = file(self.filename, 'rb')
        except IOError, msg:
            raise EncodingError(str(msg))
        try:
            transfer_data(input.read, output.write)
        finally:
            input.close()
            output.close()

    @classmethod
    def from_aiff(cls, filename, aiff_filename, compression=None,
                  progress=None):
        try:
            cls(aiff_filename).verify()
        except InvalidAIFF, err:
            raise EncodingError(unicode(err))

        try:
            input = file(aiff_filename, 'rb')
            output = file(filename, 'wb')
        except IOError, err:
            raise EncodingError(str(err))
        try:
            total_bytes = os.path.getsize(aiff_filename)
            current_bytes = 0
            s = input.read(4096)
            while (len(s) > 0):
                current_bytes += len(s)
                output.write(s)
                if (progress is not None):
                    progress(current_bytes, total_bytes)
                s = input.read(4096)
            output.flush()
            try:
                return AiffAudio(filename)
            except InvalidFile:
                cls.__unlink__(filename)
                raise EncodingError(u"invalid AIFF source file")
        finally:
            input.close()
            output.close()

    def convert(self, target_path, target_class, compression=None,
                progress=None):
        """Encodes a new AudioFile from existing AudioFile.

        Take a filename string, target class and optional compression string.
        Encodes a new AudioFile in the target class and returns
        the resulting object.
        May raise EncodingError if some problem occurs during encoding."""

        if (hasattr(target_class, "from_aiff")):
            return target_class.from_aiff(target_path,
                                          self.filename,
                                          compression=compression,
                                          progress=progress)
        else:
            return target_class.from_pcm(target_path,
                                         to_pcm_progress(self, progress),
                                         compression)

    def pcm_split(self):
        """Returns a pair of data strings before and after PCM data.

        The first contains all data before the PCM content of the data chunk.
        The second containing all data after the data chunk.
        For example:

        >>> a = audiotools.open("input.aiff")
        >>> (head, tail) = a.pcm_split()
        >>> f = open("output.aiff", "wb")
        >>> f.write(head)
        >>> audiotools.transfer_framelist_data(a.to_pcm(), f.write, True, True)
        >>> f.write(tail)
        >>> f.close()

        should result in "output.aiff" being identical to "input.aiff".
        """

        from .bitstream import BitstreamReader
        from .bitstream import BitstreamRecorder

        head = BitstreamRecorder(0)
        tail = BitstreamRecorder(0)
        current_block = head

        aiff_file = BitstreamReader(open(self.filename, 'rb'), 0)
        try:
            #transfer the 12-byte "RIFFsizeWAVE" header to head
            (form, size, aiff) = aiff_file.parse("4b 32u 4b")
            if (form != 'FORM'):
                raise InvalidAIFF(_(u"Not an AIFF file"))
            elif (aiff != 'AIFF'):
                raise InvalidAIFF(_(u"Invalid AIFF file"))
            else:
                current_block.build("4b 32u 4b", (form, size, aiff))
                total_size = size - 4

            while (total_size > 0):
                #transfer each chunk header
                (chunk_id, chunk_size) = aiff_file.parse("4b 32u")
                if (not frozenset(chunk_id).issubset(self.PRINTABLE_ASCII)):
                    raise InvalidAIFF(_(u"Invalid AIFF chunk ID"))
                else:
                    current_block.build("4b 32u", (chunk_id, chunk_size))
                    total_size -= 8

                #round up chunk size to 16 bits
                if (chunk_size % 2):
                    chunk_size += 1

                #and transfer the full content of non-data chunks
                if (chunk_id != "SSND"):
                    current_block.write_bytes(aiff_file.read_bytes(chunk_size))
                else:
                    #transfer alignment as part of SSND's chunk header
                    align = aiff_file.parse("32u 32u")
                    current_block.build("32u 32u", align)
                    aiff_file.skip_bytes(chunk_size - 8)
                    current_block = tail

                total_size -= chunk_size

            return (head.data(), tail.data())
        finally:
            aiff_file.close()

    def has_foreign_aiff_chunks(self):
        return (set(['COMM', 'SSND']) != set([c.id for c in self.chunks()]))

    def verify(self, progress=None):
        """Verifies the current file for correctness.

        Returns True if the file is okay.
        Raises an InvalidFile with an error message if there is
        some problem with the file."""

        #AIFF chunk verification is likely to be so fast
        #that individual calls to progress() are
        #a waste of time.

        COMM_found = False
        SSND_found = False

        for chunk in self.chunks():
            if (chunk.id == "COMM"):
                if (not COMM_found):
                    COMM_found = True
                else:
                    raise InvalidAIFF(_(u"multiple COMM chunks found"))

            elif (chunk.id == "SSND"):
                if (not COMM_found):
                    raise InvalidAIFF(_(u"SSND chunk found before fmt"))
                elif (SSND_found):
                    raise InvalidAIFF(_(u"multiple SSND chunks found"))
                else:
                    SSND_found = True

            if (not chunk.verify()):
                raise InvalidAIFF(_(u"truncated %s chunk found") %
                                  (chunk.id.decode('ascii')))

        if (not COMM_found):
            raise InvalidAIFF(_(u"COMM chunk not found"))
        if (not SSND_found):
            raise InvalidAIFF(_(u"SSND chunk not found"))

        if (progress is not None):
            progress(1, 1)

        return True

    def clean(self, fixes_performed, output_filename=None):
        """Cleans the file of known data and metadata problems.

        fixes_performed is a list-like object which is appended
        with Unicode strings of fixed problems

        output_filename is an optional filename of the fixed file
        if present, a new AudioFile is returned
        otherwise, only a dry-run is performed and no new file is written

        Raises IOError if unable to write the file or its metadata
        Raises ValueError if the file has errors of some sort
        """

        chunk_queue = []
        pending_data = None

        for chunk in self.chunks():
            if (chunk.id == "COMM"):
                if ("COMM" in [c.id for c in chunk_queue]):
                    fixes_performed.append(
                        _(u"multiple COMM chunks found"))
                else:
                    chunk_queue.append(chunk)
                    if (pending_data is not None):
                        chunk_queue.append(pending_data)
                        pending_data = None
            elif (chunk.id == "SSND"):
                if ("COMM" not in [c.id for c in chunk_queue]):
                    fixes_performed.append(_(u"SSND chunk found before fmt"))
                    pending_data = chunk
                elif ("SSND" in [c.id for c in chunk_queue]):
                    fixes_performed.append(_(u"multiple SSND chunks found"))
                else:
                    chunk_queue.append(chunk)
            else:
                chunk_queue.append(chunk)

        old_metadata = self.get_metadata()
        if (old_metadata is not None):
            fixed_metadata = old_metadata.clean(fixes_performed)
        else:
            fixed_metadata = old_metadata

        if (output_filename is not None):
            AiffAudio.aiff_from_chunks(output_filename, chunk_queue)
            fixed_aiff = AiffAudio(output_filename)
            if (fixed_metadata is not None):
                fixed_aiff.update_metadata(fixed_metadata)
            return fixed_aiff
