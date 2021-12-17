#!/usr/bin/env python3

"""
ec_978 - Error Correct FIS-B and ADS-B messages.
================================================

Accepts packets (either FIS-B or ADS-B), usually sent from
``demod-978`` into standard input. 

The input is sent as two messages, the first is a 30 byte character
string which provides information about the data packet to follow.
This is then followed by a datapacket as bytes suitable as a numpy
I32 array. Two sizes of packets are sent (and indicated as such in the 
preceding 30 byte attribute packet): ``PACKET_LENGTH_FISB`` (35340)
for FIS-B packets and ``PACKET_LENGTH_ADSB`` (3084) for ADS-B packets.

Note that while there are two types of ADS-B packets, short and long, that
the length is set for long packets. That allows for long and short packets
to be error corrected (you can sort of, but not absolutely, distinguish between the
two at the time they are sent).

After the packets are received, they are packed together into bytes 
and error corrected using the reed-solomon parity bits. If the initial error correction
is not successful, the bits before and after each bit are used in a
set of weighted averages to come up with a new set of bits. These bits
are then tried. The concept is that since we are sampling at the
Nyquist frequency, the bits actually sample are usually not at optimum
sampling points. By using their 'bit buddies' (neighbor bits) we can
come up with more optimum sampling points. Empirically, this is
very effective.

Successful corrections are passed to standard output.
"""

import sys
import os
import os.path
import numpy as np
import pyreedsolomon as rs
import argparse
import glob
import time
import shutil
from argparse import RawTextHelpFormatter

# List of positions to shift bits by to error correct messages.
#
# Positive values means you use the bits before, negative values means you use
# the bits after. These are the most likely values to result in a match
# ordered by the likeliness (empirically determined).
# 
# This list was created a large number of samples. We found the amount that
# would error correct the largest percentage of packets, and then removed those
# packets from contention. The process was then repeated. It was also found
# that a granularity of less than 5% wasn't useful.
SHIFT_BY_PROBABILITY = [0, -0.75, 0.75, -0.50, 0.50, -0.25, \
      0.25, -0.85, 0.40, 0.65, -0.30, 0.80, -.05, 0.05, -0.90, 0.90, -0.10, \
      0.10, 0.85, -0.15, 0.15, -0.80, -0.65, -0.35, 0.35, -0.70, 0.70, \
      0.30, -0.40, -0.60, 0.60, -0.20, 0.20, -0.45, 0.45, -0.55, 0.55]

# Number of bits in a raw FIS-B data block (with RS bits)
BITS_PER_BLOCK = 4416

# Number of bytes in a raw FIS-B data block (with RS bits)
BYTES_PER_BLOCK = BITS_PER_BLOCK // 8

#: Length of attribute string sent with each packet.
ATTRIBUTE_LEN = 30

#: Size of a FIS-B packet in bytes.
#: Derived from ((4416 * 2) + 3) * 4.
#: FIS-B packet has 4416 bits * 2 for 2 samples/bit + 3 for extra
#: bit before actual data and 2 after. Multiplied by 4 since each
#: sample is 4 bytes long.
PACKET_LENGTH_FISB = 35340

#: Size of an ADS-B packet in bytes.
#: Derived from ((384 * 2) + 3) * 4.
#: ADS-B long packet has 384 bits * 2 for 2 samples/bit + 3 for extra
#: bit before actual data and 2 after. Multiplied by 4 since each
#: sample is 4 bytes long. We consider all ADS-B packets as long
#: since this will capture all cases (long and short ADS-B).
PACKET_LENGTH_ADSB = 3084

# If True, show information about failed FIS-B packets as a comment.
show_failed_fisb = False

# If True, show information about failed ADS-B packets as a comment.
show_failed_adsb = False

# Directory and flag for writing error files.
dir_out_errors = None
writingErrorFiles = False

# Flag to show lowest usable signal levels. Set by --ll.
show_lowest_levels = False

# Reed-Solomon error correction Ground Uplink parameters:
#   Symbol Size: 8
#   Message Symbols: 72 (ADS-B short: 144, ADS-B long: 272)
#   Message + ECC symbols: 92 (ADS-B short: 240, ADS-B long: 384)
#   Galois field generator polynomial coefficients: 0x187
#   fcr (First Consecutive Root): 120
#   Primitive element: 1
#   Number of roots: 20 (ADS-B short: 12, ADS-B long: 14)
#    (same as the number of parity bits)
rsAdsbS = rs.Reed_Solomon(8,18,30,0x187,120,1,12)
rsAdsbL = rs.Reed_Solomon(8,34,48,0x187,120,1,14)
rsFisb = rs.Reed_Solomon(8,72,92,0x187,120,1,20)

def block0ThoroughCheck(hexBlocks):
  """
  Look at all consecutive blocks starting with block 0 and see
  if the packet has ended and the rest of the packet is
  zero-filled. If so, ``hexBlocks`` will be altered and all
  remaining blocks will be filled in.

  Args:
    hexBlocks (list): List of 6 hex blocks, each containing a string of 
      hex values if already error corrected, or ``None`` is not error corrected.
      
  Returns:
    tuple: Tuple containing:

    * ``True`` if this packet matched. Else ``False``.
      ``True`` indicates the packet is entirely error corrected.
    * If this was a successful error correction, ``hexBlocks`` is altered
      to contain values for all 6 blocks. Otherwise, the value that
      ``hexBlocks`` contained when the function was called is
      returned.
  """
  
  # If we don't have a block 0, we can't continue
  if hexBlocks[0] == None:
    return False, hexBlocks
  
  # Gather all the consecutive blocks together, starting with block 0
  hexData = hexBlocks[0]
  lastBlock = 0
  for i in range(1,5):
    if hexBlocks[i] != None:
      hexData += hexBlocks[i]
      lastBlock += 1
    else:
      # Stop at the first non-consecutive block
      break

  # Turn hex string back to bytes.
  dataBytes = bytearray.fromhex(hexData)
  dataBytesLen = len(dataBytes)

  # Jump through UAT Frames, looking for an empty frame.
  bytePtr = 8

  # Loop until we have looped over all packets we have.
  while True:
    if bytePtr + 1 >= dataBytesLen:
      break

    uatFrameLen = (dataBytes[bytePtr] << 1) | (dataBytes[bytePtr + 1] >> 7)

    if uatFrameLen == 0:
      # Found end of frame, so we matched.
      currentBlock = (bytePtr + 1) // 72

      # Set all future blocks to zeroes.
      for i in range(currentBlock+1, 6):
        hexBlocks[i] = '00' * 72
      
      return True, hexBlocks

    # Have a next frame, try to proceed to it
    bytePtr += uatFrameLen + 2

  # Didn't find anything
  return False, hexBlocks

def packAndTest(rs, bits):
  """
  Take a set of integers, each integer representing a
  single bit, and turn them into binary and pack them into
  bytes. Then check them with the Reed-Solomon error correction object.

  Args:
    rs (reed-solomon object): Reed-Solomon object to use.
      This object is different for FIS-B, 
      ADS-B short, and ADS-B long.
    bits (nparray): int32 array containing one integer per bit
      for the one FIS-B block or an entire ADS-B message.

  Returns:
    tuple: Tuple containing:

    * String of hex if the error correction was successful, else ``None``.
    * Number of errors detected. Will be from 0 to 4 if a success.
      Otherwise, will be a negative number.
  """
  # Turn integers to 1/0 bits and pack in bytes.
  byts = np.packbits(np.where((bits > 0), 1, 0))
  
  # Do Reed-Solomon error correction.
  errCorrected, errs = rs.decode(byts)
  
  if errs >= 0:
    errCorrectedHex = errCorrected.tobytes().hex()
  else:
    errCorrectedHex = None

  return errCorrectedHex, errs

def extractBlockBitsFisb(samples, offset, blockNumber):
  """
  Given a block number, extract an integer array of data bits for the
  packet as supplied, as well as arrays for one sample before and 
  one sample after the normal packet. Applies to FIS-B only.

  Args:
    samples (nparray): Demodulated samples (int32).
    offset (int): This is the offset into ``samples`` where we consider.
      the 'starting' bit to be. It is usually one, although if we don't 
      error correct with 1, 2 will be used later. 1 gets the packet that we error corrected
      the sync word to. 2 will get the packet as the set of bits following
      the packet that we got the sync word for.
    blockNumber (int): Block number we are decoding (0-5).

  Returns:
    tuple: Tuple containing:

    * Array of integers (nparray i32) representing the actual error corrected packet.
    * Array of integers (nparray i32) containing 1 bit before the first returned
      result.
    * Array of integers (nparray i32) containing 1 bit after the first returned
      result.
  """
  # Arrays where bits will be placed.
  bitsHolder = np.empty((92,8), dtype=np.int32)
  bitsBeforeHolder = np.empty((92, 8), dtype=np.int32)
  bitsAfterHolder = np.empty((92,8), dtype=np.int32)
  
  # Start bit (and 'samples' running pointer)
  samplesPtr = offset + ((8 * blockNumber) * 2)
  
  # Each block contains 92 data words.
  for i in range(0, 92):
    # Extract the bits for a single 8-bit word.
    bitsHolder[i] = samples[samplesPtr: samplesPtr+16: 2]
    bitsBeforeHolder[i] = samples[samplesPtr-1: samplesPtr + 16 - 1 :2]
    bitsAfterHolder[i] = samples[samplesPtr+1: samplesPtr + 16 + 1 :2]

    # Data is interleaved. This will deinterleave the block.
    samplesPtr += 96 # skip to next byte in block (5 * 8 * 2) + 16

  # Array is a list of 92 items with each element containing an array
  # of 8 integers. The reshape calls will turn this into a single list
  # of 92 * 8 integers.
  return np.reshape(bitsHolder, -1), np.reshape(bitsBeforeHolder, -1), \
         np.reshape(bitsAfterHolder, -1)
  
def extractBlockBitsAdsb(samples, offset, isShort):
  """
  Extract an integer array of data bits for the
  packet as supplied, as well as arrays for one sample before, and 
  one sample after the normal packet. Applies to ADS-B only.

  ADS-B packets come in two flavors, short and long. Most are
  long, but short packets have the first 5 bits set to zero. This gives
  a good first indication of which one to try first, but because of errors,
  both will be tried. There is about a 10:1 ratio of long to short packets.

  Args:
    samples (nparray): Demodulated samples (int32).
    offset (int): This is the offset into ``samples`` where we consider
      the 'starting' bit to be. It is usually one, although if we don't 
      error correct with 1, 2 will be used later. 1 gets the packet that we extracted
      the sync word from. 2 will get the packet as the set of bits following
      the packet that we got the sync word for.
    isShort (bool): ``True`` if we are attempting to error correct a short ADS-B
      packet, or ``False`` if this a long packet attempt.

  Returns:
    tuple: Tuple containing:

    * Array of integers (nparray i32) representing the actual error corrected packet.
    * Array of integers (nparray i32) containing 1 bit before the first returned
      result.
    * Array of integers (nparray i32) containing 1 bit after the first returned
      result.
  """
  # Long packet is 48 bytes.
  numBytes = 48

  if isShort:
    # Short packet is 30 bytes.
    numBytes = 30
  
  # Create the samples
  bits = samples[offset:(numBytes * 16):2]
  bitsBefore = samples[offset-1:(numBytes * 16) - 1:2]
  bitsAfter = samples[offset+1:(numBytes * 16) + 1:2]
  
  return bits, bitsBefore, bitsAfter

def shiftBits(bits, neighborBits, shiftAmount):
  """
  Given a set of packet bits, and either bits that
  came before or after the normal packet bits, multiply
  each neighbor bit by a percentage and add it to the
  packet bit.

  This is the magic behind better error correction. Since we are
  sampling at the Nyquist rate, we can either be sampling
  near the optimum sampling points, or the worst sampling
  points. We use neighbor samples to influence where we
  sample and provide alternate set of values to try to
  error correct closer to the optimum sampling point.

  For really strong signals, this doesn't have much effect,
  most packets will error correct well. But for weaker signals,
  this can provide close to a doubling of packets error corrected.

  Shift amounts are found in ``SHIFT_BY_PROBABILITY`` and
  are ordered by what shift values will produce the quickest
  error corrections (using lots of empirical samples). The first shift 
  is always 0, since this is most likely to match.

  You can also get a good first approximation by determing
  zero-crossing values, but using the shift table is
  essentially just as fast.

  Args:
    bits (nparray): Sample integers array.
    neighborBits (nparray): Sample integers array of samples either
      before (``bitsBefore``) or after (``bitsAfter``) the sample.
    shiftAmount (float): Amount to shift bits toward the 
      ``neighborBits`` as a percentage.

  Returns:
    nparray:  ``bits`` array after shifting.
  """
  # Add a percentage of a neighbor bit to the sample bit.
  # Samples are positive and negative numbers, so this
  # will either raise or lower the sample point.
  shiftedBits = (bits + (neighborBits * shiftAmount))/2
  
  return shiftedBits

def tryShiftBits(rs, bits, bitsBefore, bitsAfter, tryFirst):
  """
  Given 3 packets of data (sample, before sample, and
  after sample), error correct the sample using various shifts.

  The bits represent a single block of a FIS-B message,
  or the entire ADS-B message.

  Args:
    rs (reed-solomon object): Reed-Solomon object to use.
      One of three instances are passed dependent on the
      type of packet being error corrected (FIS-B, ADS-B short,
      ADS-B long).
    bits (nparray): Integers representing the current packet.
    bitsBefore (nparray): Integers representing the bits
      before the current packet.
    bits (nparray): Integers representing the bits
      after the current packet.
    tryFirst (int): Try this shift first. Is ignored if this -1.
      This is used for FIS-B packets to set the shift to one
      that worked for a previous shift.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * String of hex if the error correction was successful, else ``None``.
    * Number of errors found. Will be 4 or less for successfull
      error correction or 99 for a failed correction.
    * Number of shifts that were tried before a successful decode. 1 means
      we decoded on the first try. -1 sent if the decode was not a success.
    * Shift value that was successful, or -1 if not successful. Used as
      tryFirst on the next run.
  """
  # Record the number of times we tried a shift.
  numTries = 0

  if tryFirst != -1:
    if tryFirst == 0:
      shiftedBits = bits
    elif tryFirst > 0:
      shiftedBits = shiftBits(bits, bitsBefore, tryFirst)
    else:
      shiftedBits = shiftBits(bits, bitsAfter, -tryFirst)

    numTries += 1

    errCorrectedHex, errs = packAndTest(rs, shiftedBits)
    if errs >= 0:
      return True, errCorrectedHex, errs, numTries, tryFirst

  # Try all shifts in ``SHIFT_BY_PROBABILITY`` list.
  for shift in SHIFT_BY_PROBABILITY:
    # Don't try a shift we already tried
    if shift == tryFirst:
      continue

    if shift == 0:
      shiftedBits = bits
    elif shift > 0:
      shiftedBits = shiftBits(bits, bitsBefore, shift)
    else:
      shiftedBits = shiftBits(bits, bitsAfter, -shift)

    numTries += 1

    errCorrectedHex, errs = packAndTest(rs, shiftedBits)
    if errs >= 0:
      return True, errCorrectedHex, errs, numTries, shift

  return False, None, 99, -1, -1

def processAndRepairFisb(samples, offset, hexBlocks, hexErrs):
  """
  Given a FIS-B raw message, attempt to error correct all blocks.

  We loop over each block and determine the bits, bits before, and
  bits after. We then attempt to error correct using 
  ``tryShiftBits()`` which will try different combinations.

  We also employ various procedures such as seeing if a packet
  ends early (like an empty packet) where we do not need to error correct
  further blocks.

  Args:
    samples (nparray): Int 32 array of samples containing the bit
      before the actual sample and two bits after the normal sample.
    offset (int): The normal value is 1, which uses the starting bit (``samples[1]``)
      of the normal sample. Can also be 2, which looks at the set of bits
      after the current sample (``samples[2]``). Using this can result in a small, but
      significant increase, in error corrections.
    hexBlocks (list): 6 item list containing 1 hex string for each
      block, or ``None``. When first called with an offset of 1, set hexBlocks
      to a single ``None`` value.
    hexErrs (list): List of 6 items containing 1 integer representing
      the error count for each FIS-B block. When first called with an offset of 
      1, provide a single ``None`` value. ``99`` represents the number
      for too many errors.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Updated version of ``hexBlocks`` which will be a 6 item list
      containing ``None`` for blocks that did not error correct, or a
      hex string with the error corrected value.
    * Updated version of ``hexErrs`` which will be a 6 item list
      with each element being number of errors found in the block
      (0-4) or ``99`` for a block that failed to error correct.
    * Number of attempts at decode. -1 if failed.
  """
  # Create hexBlocks if first time call.
  if hexBlocks == None:
    hexBlocks = [None, None, None, None, None, None]

  # Create hexErrs if first time call.
  if hexErrs == None:
    hexErrs = [99, 99, 99, 99, 99, 99]

  numTriesTotal = 0
  shiftThatWorked = -1

  # Loop and try to error correct each block
  for block in range(0, 6):

    if hexBlocks[block] != None:
      continue

    bits, bitsBefore, bitsAfter = extractBlockBitsFisb(samples, offset, \
      block)
    
    # Shift bits
    status, errCorrectedHex, hexErrs[block], numTriesBlock, shift = tryShiftBits(rsFisb, bits, bitsBefore, bitsAfter, shiftThatWorked)

    numTriesTotal += numTriesBlock

    if status:
      # Start next block with the shift that worked.
      shiftThatWorked = shift

      hexBlocks[block] = errCorrectedHex

      # For block0, see if empty frame. We put this check here since
      # 'tryShiftBits' is the most likely opportunity to error correct a 
      # block zero, and this prevents us from even trying to error correct
      # other frames. Empty frames are very common.
      if block == 0:
        foundEmptyFrame, hexBlocks = block0ThoroughCheck(hexBlocks)
        if foundEmptyFrame:
          return True, hexBlocks, hexErrs, numTriesTotal

      continue
    
    # There are still blocks with errors

    # If we have a valid block zero, do a more thorough check of block zero
    # looking for frames that end early with zeros.
    status, hexBlocks = block0ThoroughCheck(hexBlocks)
    if status:
      return True, hexBlocks, hexErrs, numTriesTotal

  # If there are None values in hexBlocks, nothing worked.
  if None in hexBlocks:
    return False, hexBlocks, hexErrs, -1

  # Otherwise, all blocks were error corrected.
  return True, hexBlocks, hexErrs, numTriesTotal

def processAndRepairAdsb(samples, offset, isShort):
  """
  Given a ADS-B raw message, attempt to error correct all blocks.

  Args:
    samples (nparray): int32 array of samples containing the bit
      before the normal sample and two bits after the normal sample.
    offset (int): The normal value is 1, which uses the starting bit (``samples[1]``)
      of the normal sample. Can also be 2, which looks at the set of bits
      after the current sample (``samples[2]``).
    isShort (bool): ``True`` if we are trying to match a short ADS-B
      packet, otherwise ``False``.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message.
    * Number of errors found in the message ((0-4) or ``99`` for a
      message that failed to error correct.
    * Number of times Reed-Solomon decoding was tried on this packet.
      -1 if the packet did not decode.
  """
  # Short and long ADS-B use different Reed-Solomon instances.
  if isShort:
    rs = rsAdsbS
  else:
    rs = rsAdsbL

  # Create the bits to use.
  bits, bitsBefore, bitsAfter = extractBlockBitsAdsb(samples, \
    offset, isShort)
  
  # Shift bits
  status, hexBlock, errs, numTriesTotal, _ = tryShiftBits(rs, bits, bitsBefore, bitsAfter, -1)

  if status:
    return True, hexBlock, errs, numTriesTotal

  return False, None, 99, -1

def hexErrsToStr(hexErrs):
  return f'{hexErrs[0]:02}:{hexErrs[1]:02}:{hexErrs[2]:02}:{hexErrs[3]:02}:{hexErrs[4]:02}:{hexErrs[5]:02}'

def hexBlocksToHexString(hexBlocks):
  """
  Given a list of 6 items, each containing a hex-string of
  a error corrected block, return a string with all 6 hex strings
  concatinated. Used only for FIS-B.

  The only returns the bare hex-string with '+'
  prepended and no time, signal strength, or error count attached.

  Args:
    hexBlocks (list):  List of 6 strings containing a hex string for each
        of the 6 FIS-B hexblocks. Only called if all blocks error corrected
        successfully (i.e. there should be no ``None`` items).

  Returns:
    string: String with all hex data and a '+' prepended at the front.
    This function is only for FIS-B packets. No data is appended to
    the end.
  """
  return '+' + hexBlocks[0] + hexBlocks[1] + hexBlocks[2] + \
    hexBlocks[3] + hexBlocks[4] + hexBlocks[5]

def hexBlocksFormatted(hexBlocks, signalStrengthStr, timeStr, hexErrs, \
   syncErrors, numTries):
  """
  Takes error corrected FIS-B message and produces final output string.

  Will concatenate all the hex blocks and add trailing error, signal strength
  and time information. Should be called only for a successfully error corrected
  FIS-B packet.

  Args:
    hexBlocks (list): 6 item list consisting of error corrected hex string for
      each block.
    signalStrengthStr (str): Already created signal strength string.
    timeStr (str): Already created time string.
    hexErrs (list): 6 item list with each element being the number
      of Reed-Solomon errors in the block.
    syncErrors (int): Number of errors found in the sync word.
    numTries (int): Number of attempts at decodes. 1 is the best number.
      Higher numbers mean more shifts were needed.

  Returns:
    str: Final error corrected FIS-B string ready for display.
  """
  rsErrStr = hexErrsToStr(hexErrs)

  return hexBlocksToHexString(hexBlocks) + ';rs=' + str(syncErrors) + '/' + \
    rsErrStr + '/' + f'{numTries:03}' + ';' + signalStrengthStr + \
    ';' + timeStr

def hexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, \
    syncErrors, numTries):
  """
  Takes error corrected ADS-B message and produces final output string.

  Will concatenate the error corrected hex block with the trailing error, signal strength
  and time information. Should be called only for a successfully error corrected
  ADS-B packet.

  Args:
    hexBlock (str): Error corrected ADS-B message as a hex string.
    signalStrengthStr (str): Already created signal strength string.
    timeStr (str): Already created time string.
    errs (int): Number of Reed-Solomon errors detected.
    syncErrors (int): Number of errors found in the sync word.
    numTries (int): Number of Reed-Solomon attempts before decoding.

  Returns:
    str: Final error corrected ADS-B string ready for display.
  """
  return '-' + hexBlock + ';rs=' + str(syncErrors) + '/' + str(errs) + \
       '/' + f'{numTries:03};' + signalStrengthStr + ';' + timeStr

def processFisbPacket(samples, timeStr, signalStrengthStr, syncErrors, \
    attrStr):
  """
  Takes raw FIS-B packet samples and will error correct to final hex string
  to be sent to standard output.

  If we are showing errors (``-ff``), will return error string instead (error strings 
  start with ``#`` and are ignored by other FIS-B processing code).

  Args:
    samples (nparray): Demodulated packet samples consisting of FIS-B data
      as well as 1 sample before and 2 samples after.
    timeStr (str): Already created time string.
    signalStrengthStr (str): Already created signal strength string.
    syncErrors (int): Number of errors found in the sync word.
    attrStr (str): String with attributes. Used primarily for matching
      an error file to a specific packet.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message. If the message
      was not error corrected, this will be the string of hex errors for
      each block in the form: 'n:n:n:n:n:n' where n is 1-10.
      If we are displaying errors, this will be the error string.
  """
  # Starting offset is 1. This is where tha actual sample data begins.
  offset = 1

  # Start with simple sample error correction. This works most of the time.
  didErrCorrect, hexBlocks, hexErrs, numTries = processAndRepairFisb(samples, offset, None, None)

  if didErrCorrect:
    return didErrCorrect, hexBlocksFormatted(hexBlocks, \
      signalStrengthStr, timeStr, hexErrs, syncErrors, numTries)

  # Try with added offset
  didErrCorrect, hexBlocks, hexErrs, numTries = processAndRepairFisb(samples, offset + 1, hexBlocks, hexErrs)

  if didErrCorrect:
    # Return formatted string (add 500 to num tries if we needed an offset)
    return didErrCorrect, hexBlocksFormatted(hexBlocks, signalStrengthStr, \
      timeStr, hexErrs, syncErrors, numTries + 500)
  
  # Did not error correct.
  hexErrsStr = hexErrsToStr(hexErrs)

  if show_failed_fisb:

    failStr = f'#FAILED-FIS-B {syncErrors}/{hexErrsStr} {signalStrengthStr} {timeStr} ' + \
        attrStr

    # Write to standard output.
    print(failStr, flush=True)

  return False, hexErrsStr

def processAdsbPacket(samples, timeStr, signalStrengthStr, syncErrors, \
    attrStr):
  """
  Takes raw ADS-B packet samples and will error correct to final hex string
  to be sent to standard output.

  If we are showing errors (``-fa``), will return error string instead (error strings 
  start with ``#`` and are ignored by other FIS-B processing code).

  Args:
    samples (nparray): Demodulated packet samples consisting of FIS-B data
      as well as 1 sample before and 2 samples after.
    timeStr (str): Already created time string.
    signalStrengthStr (str): Already created signal strength string.
    syncErrors (int): Number of errors found in the sync word.
    attrStr (str): String with attributes. Used primarily for matching
      an error file to a specific packet.

  Returns:
    tuple: Tuple containing:

    * ``True`` if there was a successful error correction, else ``False``.
    * Hex string representing the error corrected message. If the message
      was not error corrected, this will be ``None``.
      If we are displaying errors, this will be the error string.
  """
  # Starting offset is 1. This is where tha actual sample data begins.
  offset = 1

  # Try to guess if this is a short message (1st 5 bits 0). We will try both
  # ways, but try to start with the most likely candidate.
  isShort = True #assume short
  for x in range(1, 10, 2):
    if samples[x] <= 0:
      isShort = False
      break
  
  # Error corrections are sorted by the % of the time they match. I.e. we try the
  # things most likely to work most of the time first.

  # normal 94.2%
  didErrCorrect, hexBlock, errs, numTries = processAndRepairAdsb(samples, offset, isShort)
  if didErrCorrect:
    return didErrCorrect, hexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, syncErrors, numTries)
  
  # opposite (we thought is was a long, but it was a short, or visa versa)
  # 2.9%
  didErrCorrect, hexBlock, errs, numTries = processAndRepairAdsb(samples, offset, not isShort)
  if didErrCorrect:
    return didErrCorrect, hexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, syncErrors, numTries)
  
  # opposite offset (switch long for short (or visa versa) and add offset) 2.3%
  didErrCorrect, hexBlock, errs, numTries = processAndRepairAdsb(samples, offset + 1, not isShort)
  if didErrCorrect:
    return didErrCorrect, hexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, syncErrors, numTries + 500)

  # offset (take our original data and increase the offset) 0.4%
  didErrCorrect, hexBlock, errs, numTries = processAndRepairAdsb(samples, offset + 1, isShort)
  if didErrCorrect:
    return didErrCorrect, hexBlockFormatted(hexBlock, signalStrengthStr, timeStr, errs, syncErrors, numTries + 500)

  # Did not error correct.
  if show_failed_adsb:

    failStr = f'#FAILED-ADS-B {syncErrors}/{errs} {signalStrengthStr} {timeStr} ' + \
      attrStr

    # Write to standard output.
    print(failStr, flush=True)

  return False, None

def main():
  """
  Process raw FIS-B and ADS-B demodulated samples from
  standard input (usually from ``demod_978``).

  We read a fixed length attribute string containing information
  about the packet to follow. Then we read the packet and process it.

  Rinse and repeat.

  Output is sent either to standard output. If indicated (``-se``), errors 
  can be saved seperately.
  """
  # Lowest signal level found for FIS-B and ADS-B.
  # These start out as higher than we will ever see.
  lowest_adsb_level = 1000000000
  lowest_fisb_level = 1000000000
  
  try:
    while True:
      # We alternate reading attributes and packets

      # Read attributes
      attributes = sys.stdin.buffer.read(ATTRIBUTE_LEN)

      # Convert from bytes to string for parsing.
      attrStr = "".join(chr(x) for x in attributes)

      # Exit if nothing more to read (should only happen when
      # reading from file).
      if attrStr == '':
        break

      splitName = attrStr.split(".")

      # Create time string and signal strength string from the
      # file components.
      timeStr = 't=' + splitName[0] + '.' + splitName[1][0:3]
      rawSignalStrength = round(int(splitName[3]) / 1000000.0, 2)
      signalStrengthString = 'ss=' + str(rawSignalStrength)
            
      # Extract sync errors
      syncErrors = splitName[4]

      # Detect if this an a FIS-B packet ('F') or ADS-B packet ('A').
      if (splitName[2] == 'F'):
        isFisbPacket = True
        packetLength = PACKET_LENGTH_FISB
      else:
        isFisbPacket = False
        packetLength = PACKET_LENGTH_ADSB

      # Read packet as a set of bytes
      packetBuf = sys.stdin.buffer.read(packetLength)

      # Numpy will convert the bytes to int32's
      packet = np.frombuffer(packetBuf, np.int32)

      if isFisbPacket:
        didErrCorrect, resultStr = processFisbPacket(packet, timeStr, \
          signalStrengthString, syncErrors, attrStr)
      else:
        didErrCorrect, resultStr = processAdsbPacket(packet, timeStr, \
          signalStrengthString, syncErrors, attrStr)

      if didErrCorrect:
        # If printing lowest levels, print to stderr if this is the
        # lowest so far (for ADS-B and FIS-B independently).
        if show_lowest_levels:
          if isFisbPacket:
            if rawSignalStrength < lowest_fisb_level:
              lowest_fisb_level = rawSignalStrength
              print(f'lowest FIS-B signal: {lowest_fisb_level}', flush=True, file=sys.stderr)
          else:
            if rawSignalStrength < lowest_adsb_level:
              lowest_adsb_level = rawSignalStrength
              print(f'lowest ADS-B signal: {lowest_adsb_level}', flush=True, file=sys.stderr)

        # Write to standard output.
        print(resultStr, flush=True)

      # Write error file only if we are printing an error string.
      # This lets us specify which error files to save.
      elif writingErrorFiles and \
          ((isFisbPacket and show_failed_fisb) or \
           ((not isFisbPacket) and show_failed_adsb)):

        # For a failed FIS-B packet, resultStr is a string of 
        # Reed-Solomon errors for each block. Add this to the
        # filename. This doesn't make sense for ADB-B packets
        # because the error count is always '99'.
        hexErrStr = ''
        if resultStr is not None:
          hexErrStr = '.' + resultStr

        # Write file to error directory.
        errPath = os.path.join(dir_out_errors, attrStr + hexErrStr + '.i32')
        with open(errPath, 'wb') as errFile:
          errFile.write(packetBuf)

  except KeyboardInterrupt:
    sys.exit(0)

def mainReprocessErrors(errorDir):
  """
  Reprocess any errors from the specified error directory
  (called when ``-re`` argument is given).

  This option is to reprocess any errors that are in the
  error directory. This is used to study particular messages
  and create improved techniques for decoding.

  It does not delete any files in the directory and only
  one pass is made through the directory.

  If any message cannot be error corrected, it is not written to
  any error directory (even if specified).

  It will always print FIS-B and ADS-B errors.

  Args:
    errorDir (str): Directory where the error files are.
  """
  errFiles = glob.glob(os.path.join(errorDir, '*.i32'))

  for filen in errFiles:
    # find last index of '/'
    lastSlash = filen.rfind('/')

    attrStr = filen[lastSlash + 1:]

    splitName = attrStr.split(".")

    # Create time string and signal strength string from the
    # file components.
    timeStr = 't=' + splitName[0] + '.' + splitName[1][0:3]
    signalStrengthString = 'ss=' + str(round(int(splitName[3]) \
        / 1000000.0, 2))
      
    # Extract sync errors
    syncErrors = splitName[4]

    # Detect if this an a FIS-B packet ('F') or ADS-B packet ('A').
    if (splitName[2] == 'F'):
      isFisbPacket = True
      packetLength = PACKET_LENGTH_FISB
    else:
      isFisbPacket = False
      packetLength = PACKET_LENGTH_ADSB

    with open(filen, 'rb') as filePtr:
       # Read packet as a set of bytes
      packetBuf = filePtr.read(packetLength)

    # Numpy will convert the bytes to int32's
    packet = np.frombuffer(packetBuf, np.int32)

    if isFisbPacket:
      didErrCorrect, resultStr = processFisbPacket(packet, timeStr, \
        signalStrengthString, syncErrors, attrStr)
    else:
      didErrCorrect, resultStr = processAdsbPacket(packet, timeStr, \
        signalStrengthString, syncErrors, attrStr)

    if didErrCorrect:
      # Write to standard output.
      print(resultStr, flush=True)

# Call main function
if __name__ == "__main__":

  hlpText = \
    """ec_978.py: Error correct FIS-B and ADS-B demodulated data from 'demod_978'.

Accepts FIS-B and ADS-B data supplied by 'demod_978' and send any output to
standard output. By default will not produce any error messages for
bad packets.

The '--se' argument requires a directory where errors will be stored.
This directory should be used by the '--re' argument in a future run to reprocess
the errors. When the '--se' argument is given, you need to supply either '--fa', 
'--ff' or both to indicate the type of error(s) you wish to save.

When errors are reprocessed with '--re', the '--ff' and '--fa' arguments
are automatically set, and any '--se' argument is ignored."""
  parser = argparse.ArgumentParser(description= hlpText, \
          formatter_class=RawTextHelpFormatter)
          
  parser.add_argument("--ff", help='Print failed FIS-B packet information as a comment.', action='store_true')
  parser.add_argument("--fa", help='Print failed ADS-B packet information as a comment.', action='store_true')
  parser.add_argument("--ll", help='Print lowest levels of FIS-B and ADS-B signal levels.', action='store_true')
  parser.add_argument("--se", required=False, help='Directory to save failed error corrections.')
  parser.add_argument("--re", required=False, help='Directory to reprocess errors.')

  args = parser.parse_args()

  if args.ff:
    show_failed_fisb = True

  if args.fa:
    show_failed_adsb = True

  if args.ll:
    show_lowest_levels = True

  if args.se:
    dir_out_errors = args.se
    writingErrorFiles = True

  # If reprocessing errors call mainReprocessErrors() else main()
  if args.re:
    # Writing error files doesn't work here. Unset if set
    writingErrorFiles = False

    # Since processing errors, assume -ff -fa
    show_failed_adsb = True
    show_failed_fisb = True
    
    mainReprocessErrors(args.re)
  else:
    main()
