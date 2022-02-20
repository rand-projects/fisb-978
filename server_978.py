#!/usr/bin/env python3

"""
server_978 - Send FIS-B and ADS-B messages to clients.
======================================================

Accepts string messages from standard input and will dispurse them
in a write-only fashion to any client that connects to the server.

Default port is 3333, but this can be changed with the ``--port``
option.

Caution: This server uses select with file numbers for both
sockets and standard output, so probably not work on native Windows.
"""

import os.path
import time
import argparse
from argparse import RawTextHelpFormatter
import glob
import socket
import select
import sys

#: Maximum simultaneous connections allowed.
MAX_CONNECTIONS = 10

# Default TCP port to use (changable with --port).
port = 3333

def extractWholeLine(buf, isFirstLine):
  """
  Return the next complete line of input if available.

  ``buf`` is the accumulated input buffer received from
  decode-978. The goal is to extract complete lines from
  this data and return them as output. ``buf`` is altered
  to reflect the line being removed. We are also careful
  (using ``isFirstLine``) to ensure that all lines are 
  complete lines and that the initial characters we get
  are not sent until the first newline is captured.

  Clients will only be sent complete lines, from beginning
  to end.

  Args:
    buf (list): Current output buffer.
    isFirstLine (bool): True if we have not output
      any data. This is used to throw out any partial
      first line before a newline is received. After 
      the initial trash data is thrown away, this will
      be False for the rest of the run.

  Returns:
    tuple: Tuple containing:

    * String containing a new line to send to clients, or ``None``
      if no line available.
    * Updated version of ``buf`` to reflect any changes.
    * Updated version of ``isFirstLine``. Once set to ``False``
      will remain that way.
  """
  # If we are just starting, wait until the beginning of a 
  # new line.
  if isFirstLine:
    # Have not found our first \n
    idx = buf.find('\n')
    if idx == -1:
      # Still don't have our first \n
      return None, buf, True

    # Get rid of any partial line
    if len(buf) == idx + 1:
      buf = ''
    else:
      buf = buf[idx + 1:]

  # See if we have an entire line
  idx = buf.find('\n')
  if idx == -1:
    return None, buf, False

  line = buf[:idx + 1]
  if len(buf) == idx + 1:
    buf = ''
  else:
    buf = buf[idx + 1:]

  return line, buf, False

def main():
  """
  Main server loop.

  Will start the server. If the port is not available
  (which can happen is the server is stopped and then restarted
  quickly), the program will wait until it becomes available.

  Then will listen for client connections and receive standard
  input. The input will be dispersed to the clients.
  """
  # Set if we are just starting and we don't want to output
  # anything but a whole line. So ignore first line of buffer.
  isFirstLine = True
  
  # Start socket server
  print(f'Starting server on port {port}', file=sys.stderr)
  server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  server.setblocking(0)

  # If we restart too soon after closing, we cannot
  # bind the server. So we sleep at 10 second intervals
  # until we can.
  bindFailed = False
  while True:
    try:
      server.bind(('0.0.0.0', port))
      break
    except:
      print("Cannot bind.. waiting", file=sys.stderr)
      bindFailed = True
      time.sleep(10)
  if bindFailed:
    print("Bind successful.. continuing", file=sys.stderr)

  server.listen(MAX_CONNECTIONS)

  # Dictionary to match fileno to socket.
  # Because we are using stdin and sockets, all information
  # in select() is done handling file numbers, as opposed to
  # just sockets. We use a dictionary 'filenoDict' that has
  # a file number as a key and a socket as its value.
  filenoDict = {}
  filenoDict[server.fileno()] = server

  # Save the file numbers we use the most.
  filenoServer = server.fileno()
  filenoStdin = sys.stdin.fileno()
  
  # Setup inputs, outputs
  inputs = [filenoServer, filenoStdin]
  outputs = []
  
  # Buffer read from stdin and eventually written to stdout.
  stdinBuffer = ''

  try:
    while inputs:
      readable, writable, exceptional = \
        select.select(inputs, outputs, inputs, 0.1)

      for fno in readable:
        if fno is filenoServer:
          # Connection
          client, clientAddress = server.accept()
          print(f'Connection from {clientAddress}', file=sys.stderr)
          clientFileno = client.fileno()
          client.setblocking(0)

          filenoDict[clientFileno] = client
          inputs.append(clientFileno)
          outputs.append(clientFileno)

        elif fno is filenoStdin:
          # Append any input that is ready to be read.
          # 100 seems a good compromise that keeps data flowing
          # smoothly. Larger numbers make the flow more sporadically.
          stdinBuffer += sys.stdin.read(100)

        else:
            # Some other socket
            sock = filenoDict[fno]

            # Read data, but we don't care or use the data.
            # We can get an RST packet, so catch that.
            rstCloseType = False
            try:
              data = sock.recv(10)
            except ConnectionResetError:
              data = None
              rstCloseType = True
              
            # If no data, close connection.
            if not data:
              if rstCloseType:
                closeString = 'RST disconnect from client'
              else:
                closeString = 'Disconnect from client'
              print(closeString, file=sys.stderr)
              sock.close()

              outputs.remove(fno)
              inputs.remove(fno)
              del filenoDict[fno]

      # Send only a line at a time and don't send any lines (including the first)
      # that are not complete lines.
      line, stdinBuffer, isFirstLine = extractWholeLine(stdinBuffer, isFirstLine)

      # Sleep for 1ms if nothing to do. Leaving this out causing runtine
      # to increase to 100%
      if line == None:
        time.sleep(0.001)

      for fno in writable:
        # Write data to clients.
        if (line != None) and (fno is not filenoStdin) \
              and (fno is not filenoServer):
          
          # fno can get deleted before getting here, so double check.
          if fno in filenoDict:
            sock = filenoDict[fno]
            sock.sendall(line.encode())

      for fno in exceptional:
        if fno is not filenoStdin:
          sock = filenoDict[fno]
          print(f'Exception from {sock.getpeername()}', file=sys.stderr)
          sock.close()

          outputs.remove(fno)
          inputs.remove(fno)
          del filenoDict[fno]

  # Exit gracefully for ^C
  except KeyboardInterrupt:
    # Close all sockets
    for fno in inputs:
      # Only sockets are in filenoDict. 
      # Skip server for now, we will close it last.
      if fno in filenoDict:
        if fno != filenoServer:
          # Close client
          sock = filenoDict[fno]
          sock.shutdown(socket.SHUT_RDWR)
          sock.close()

    # Close server last.
    server.shutdown(socket.SHUT_RDWR)
    server.close()
    sys.exit(0)

# Call main function
if __name__ == "__main__":

  hlpText = \
    """server-978: Server for sending FIS-B and ADS-B messages.

Takes standard input (usually supplied by 'ec_978.py') and will
send it to any connections. Will run until interrupted.

All data is sent in complete lines."""
  parser = argparse.ArgumentParser(description= hlpText, \
          formatter_class=RawTextHelpFormatter)
  
  parser.add_argument("--port", type=int, required=False, help='Port number to use.')

  args = parser.parse_args()

  if args.port:
    port = args.port

  main()
