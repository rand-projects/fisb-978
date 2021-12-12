**FIS-B 978** ('fisb-978') is a set of programs that demodulates
and error corrects FIS-B
(Flight Information System - Broadcast) and ADS-B
(Automatic Dependent Surveillance - Broadcast)
messages transmitted on 978 Mhz, mostly in the United States.

It performs the same function as
FlightAware's 978 <https://github.com/flightaware/dump978>,
but is spread out over several programs that are piped together.
It's main advantage over FlightAware's 978 and other 'dump-978'
clones is that for moderate to weaker signals it can provide
much higher packet decode rates, since it takes into account
that samples at the Nyquist sampling rate may not be optimal
and will shift them to more optimal levels.

For strong signals, 'dump-978' and 'fisb-978' will perform similarly.

'fisb-978' is composed of two main parts and one optional part:

* **demod_978** is a C language program which takes raw data
  from an SDR program at 978 Mhz and sample rate of 2.083334 Mhz
  (with 16-bit complex integer output (CS16)) and will demodulate
  the data, detect sync words, and output packets along with attributes
  of the packet to standard output. Typically, 'ec-978.py' will
  read this data.
* **ec_978.py** takes the data from 'demod_978' and will use Reed-Solomon
  error correction to produce an output packet as hex bytes (similar to
  'dump-978'). It uses a number of techniques to provide a higher
  packet decode rate. The output is sent to standard output, where is
  can be used as is, or processed by 'server_978'.
* **server_978.py** takes the output from 'ec_978.py' and will provide
  a TCP server where clients can connect and receive the data. It is
  optional if you don't want to serve the data via TCP.

The program is broken into parts for modularity and speed. C is very
fast for searching through the input stream and finding sync packets
(numpy is great at the demodulation part, but horrid at searching
for sync codes).
Python using numpy (and a Reed-Solomon library linked to a C library),
is very quick at error correcting the packets and manipulating them
to provide more optimum bit levels for better error correction rates.

A couple of things to consider before using:

* Since 'demod_978.c' uses type-punning, a compiler that is friendly to
  that is required. GCC is such a compiler. All code expects little-endian
  byte order. This will work on most common architectures in use today.
  If needed, big-endian can be added as a future feature.

* 'server_978.py' uses a `select()` statement using both sockets and
  file I/O. As such, this will usually not work on Windows (it should
  work fine if you are using the 'Linux Subsystem for Linux').

FIS-B 978 is not designed, nor intended,
to be used for any purpose other than fun exploration. 

| :warning: WARNING                                                      |
|:-----------------------------------------------------------------------|
| FIS-B 978 is **NOT** intended for actual flight use, nor to be used as a component of a system for any flight related operations (flight planning, etc). It is strictly a fun hobby program with many bugs. It is based loosely on draft standards, not actual standards, and has not undergone any formal testing. **DO NOT USE OTHER THAN FOR FUN-- NOT FLIGHT!!** |


To get started with FIS-B 978, see the file
'``fisb-978-intro.pdf``' in the ``fisb-978/docs`` directory.
