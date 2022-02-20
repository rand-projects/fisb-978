CC=gcc
CFLAGS=-I. -O3 -Wall -funroll-loops -lm
DEPS = 
OBJ = demod_978.o

%.o: %.c $(DEPS)
	$(CC) -c -o $@ $< $(CFLAGS)

demod_978: $(OBJ)
	$(CC) -o $@ $^ $(CFLAGS)

clean:
	rm -f demod_978.o demod_978 \#* *~ .gitignore~
