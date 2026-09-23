import os
import time
import speech_recognition as sr

"""
microphones = sr.Microphone.list_microphone_names()
for index, name in enumerate(microphones):
    print(f'Microphone with index {index} and name "{name}" found')
"""

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN_FILE = os.path.join(HERE, "listen.txt")
RESPONSE_FILE = os.path.join(HERE, "response.txt")

MIC_INDEX = 4

r = sr.Recognizer()

LEAD_INS = [
    "my name is",
    "my names",
    "the name is",
    "i am called",
    "i'm called",
    "you can call me",
    "call me",
    "i am",
    "i'm",
    "im",
    "it is",
    "it's",
    "its",
    "this is",
    "hi",
    "hello",
    "hey",
]


def extract_name(text):
    cleaned = text.lower().strip().strip(".!?,")

    changed = True
    while changed:
        changed = False
        for phrase in sorted(LEAD_INS, key=len, reverse=True):
            if cleaned.startswith(phrase + " "):
                cleaned = cleaned[len(phrase) + 1 :].strip()
                changed = True
                break

    if cleaned == "":
        return None

    words = cleaned.split()
    if len(words) > 2:
        words = words[-1:]

    return " ".join(w.capitalize() for w in words)


def read_listen_file():
    try:
        with open(LISTEN_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


MAX_LISTEN_ATTEMPTS = 2


def listen_for_name(person_id):
    mic = (
        sr.Microphone() if MIC_INDEX is None else sr.Microphone(device_index=MIC_INDEX)
    )

    for attempt in range(1, MAX_LISTEN_ATTEMPTS + 1):
        with mic as source:
            r.adjust_for_ambient_noise(source)
            print("Listening for the name of person " + person_id + "...")
            audio = r.listen(source, timeout=10, phrase_time_limit=8)
            print("Stop listening")

        try:
            text = r.recognize_google(audio)
        except sr.UnknownValueError:
            if attempt < MAX_LISTEN_ATTEMPTS:
                print("Didn't catch that, listening again...")
                continue
            raise

        print("Heard: " + text)
        name = extract_name(text)
        print("Name: " + str(name))
        return name


def main():
    print("Name listener running. Waiting on " + LISTEN_FILE + " (Ctrl-C to stop)")
    quit_noted = False
    try:
        while True:
            command = read_listen_file()

            if command == "quit":
                if not quit_noted:
                    print(
                        "Greeter not running yet (stale quit). Waiting for it to come up; "
                        "Ctrl-C to stop the listener."
                    )
                    quit_noted = True
                time.sleep(1.0)
                continue
            quit_noted = False

            if command.startswith("listen"):
                parts = command.split()
                person_id = parts[1] if len(parts) > 1 else "0"

                try:
                    name = listen_for_name(person_id)
                except Exception as e:
                    print("An error occurred (" + type(e).__name__ + "): " + str(e))
                    name = None

                if name:
                    with open(RESPONSE_FILE, "w") as f:
                        f.write(person_id + "|" + name)

                while read_listen_file().startswith("listen"):
                    time.sleep(0.25)

            time.sleep(0.25)
    except KeyboardInterrupt:
        print("Listener stopped by user.")


if __name__ == "__main__":
    main()
