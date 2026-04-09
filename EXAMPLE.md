# Example Session

Below is a realistic example session demonstrating the coding assistant in action.

## Starting the Assistant

```
$ python coding_assistant.py --cwd ~/my-project --approval ask
Coding Assistant (model=qwen3.5:4b, session=20250401-143022-a1b2c3)
Type /help for commands.
```

## Step 1: List Files and Read the README

```
you> List the files in this project and read the README

  [1/8] list_files(path='.')

  [2/8] read_file(path='README.md')

assistant> Here's an overview of the project:

**Files:**
- README.md
- setup.py
- src/
  - main.py
  - utils.py
- tests/
  - test_main.py

**README.md summary:**
This is a Python web scraper project that collects data from public APIs.
It uses the `requests` library and stores results in JSON format.
```

## Step 2: Create a Hello World Script

```
you> Create a hello world Python script called hello.py

  [1/8] write_file(path='hello.py', content='#!/usr/bin/env python3\n...')

[approval required] write_file({
  "path": "hello.py",
  "content": "#!/usr/bin/env python3\n\"\"\"Simple hello world script.\"\"\"\n\n\ndef main():\n    print(\"Hello, world!\")\n\n\nif __name__ == \"__main__\":\n    main()\n"
})
Allow? [y/N] y

assistant> I've created `hello.py` with a simple hello world script. It includes
a `main()` function and the standard `if __name__` guard.
```

## Step 3: Run the Script

```
you> Run the hello.py script

  [1/8] shell(command='python3 hello.py')

[approval required] shell({
  "command": "python3 hello.py"
})
Allow? [y/N] y

assistant> The script ran successfully:

```
Hello, world!
```

The output confirms everything is working correctly.
```

## Using Memory and Session Commands

```
you> /memory
- Wrote file: hello.py
- Ran command: python3 hello.py

you> /session
/home/user/my-project/.coding-assistant/sessions/20250401-143022-a1b2c3.json

you> /exit
```

## Resuming a Session

```
$ python coding_assistant.py --cwd ~/my-project --resume
Resumed session 20250401-143022-a1b2c3 (6 history entries)
Coding Assistant (model=qwen3.5:4b, session=20250401-143022-a1b2c3)
Type /help for commands.

you> What did we do last time?

assistant> In our previous session, I:
1. Listed the project files
2. Read the README.md
3. Created a hello.py script with a simple hello world program
4. Ran the script successfully — it printed "Hello, world!"
```
