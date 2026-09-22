#! /usr/bin/env python
# -*- encoding: UTF-8 -*-

import qi
import time
import os
import sys
import threading
import argparse


HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN_FILE = os.path.join(HERE, "listen.txt")
RESPONSE_FILE = os.path.join(HERE, "response.txt")
ROBOT_IP = "10.42.0.109"

NAME_TIMEOUT = 20.0
POLL_INTERVAL = 0.5
CLOSE_DISTANCE_M = 1.5
DEPARTURE_FORGET_S = 5.0
GOODBYE_GRACE_PERIOD = 1.0


class HumanGreeter(object):
    def __init__(self, app):
        super(HumanGreeter, self).__init__()
        # reset the handshake before anything that can block on the robot, so
        # a stale "quit" from a previous shutdown never kills a listener that
        # is already up; lets the two be started in any order.
        self.clear_files()
        app.start()
        session = app.session

        self.memory = session.service("ALMemory")

        # mac client libqi (2.8.x) can't subscribe to events on this 2.5.7 robot
        # (memory.subscriber() raises Invalid signature, subscribeToEvent
        # silently no-ops), so watch PeoplePerception/PeopleList and diff to
        # synthesize JustArrived / JustLeft locally.
        self.tts = session.service("ALTextToSpeech")
        self.motion = session.service("ALMotion")
        self.robot_posture = session.service("ALRobotPosture")
        self.awareness = session.service("ALBasicAwareness")

        try:
            self.stand_up()

            self.awareness.setEngagementMode("FullyEngaged")
            self.awareness.setTrackingMode("Head")
            self.awareness.startAwareness()
            print("Awareness on: FullyEngaged, Head tracking")

            self.names = {}
            self.greet_threads = {}
            self.pending_goodbyes = {}
            self.pending_goodbyes_lock = threading.Lock()
            self.talking = threading.Lock()
            self._people_read_ok = True

            self.start_people_watcher()
        except KeyboardInterrupt:
            print("Interrupted during start-up")
            self.shutdown()
            sys.exit(0)

    def stand_up(self):
        try:
            self.motion.wakeUp()
            self.robot_posture.goToPosture("Stand", 0.8)
            print("Pepper is up, in posture: " + self.robot_posture.getPosture())
        except Exception as e:
            print("WARNING: could not stand up: " + str(e))

    def shutdown(self):
        # safe to call from any point, even mid-__init__ when some services
        # don't exist yet; Ctrl-C must always end back in Crouch, not stiff.
        try:
            self.stop_watching.set()
        except Exception:
            pass
        awareness = getattr(self, "awareness", None)
        if awareness is not None:
            try:
                awareness.stopAwareness()
            except Exception as e:
                print("WARNING: could not stop awareness: " + str(e))
        print("Lowering Pepper back down...")
        posture = getattr(self, "robot_posture", None)
        if posture is not None:
            try:
                posture.goToPosture("Crouch", 0.6)
            except Exception as e:
                print("WARNING: could not lower to Crouch: " + str(e))
        motion = getattr(self, "motion", None)
        if motion is not None:
            try:
                motion.rest()
            except Exception as e:
                print("WARNING: could not rest: " + str(e))
        with open(LISTEN_FILE, "w") as f:
            f.write("quit")

    def read_people(self):
        # PeopleDetected is [ [ts, dur], [[id, x, y, z], ...], face stuff, count ],
        # so each person's position comes straight off the row; fall back to
        # bare PeopleList ids if the shape ever surprises us.
        try:
            data = self.memory.getData("PeoplePerception/PeopleDetected")
            self._people_read_ok = True
        except Exception as e:
            if self._people_read_ok:
                print(
                    "WARNING: could not read PeoplePerception/PeopleDetected: " + str(e)
                )
            self._people_read_ok = False
            return {}
        people = {}
        if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list):
            for person in data[1]:
                if not isinstance(person, list) or not person:
                    continue
                rid = int(person[0])
                try:
                    pos = [float(v) for v in person[1:4]]
                except Exception:
                    pos = None
                people[rid] = pos
        else:
            try:
                for rid in self.memory.getData("PeoplePerception/PeopleList"):
                    people[int(rid)] = None
            except Exception:
                pass
        return people

    def start_people_watcher(self):
        self.stop_watching = threading.Event()
        snapshot = self.read_people()
        self.people_tracks = [
            {"rid": rid, "pos": pos, "gone_at": None} for rid, pos in snapshot.items()
        ]
        present = sorted(snapshot)
        print(
            "Watching for arrivals/departures. People currently present: "
            + str(present)
        )
        if present:
            # greet people already in view at startup too, after perception
            # settles; follow each track object so an id churn mid-settle
            # doesn't silently drop the greeting.
            init = [t for t in self.people_tracks if t["rid"] in snapshot]
            init_greet = threading.Thread(target=self._greet_existing, args=(init,))
            init_greet.daemon = True
            init_greet.start()
        t = threading.Thread(target=self.watch_people)
        t.daemon = True
        t.start()

    def _greet_existing(self, init_tracks):
        time.sleep(2.0)
        greeted = []
        for tr in init_tracks:
            if tr["gone_at"] is None and tr not in greeted:
                greeted.append(tr)
                self.on_human_arrived(tr["rid"])

    def watch_people(self):
        while not self.stop_watching.is_set():
            time.sleep(POLL_INTERVAL)
            self.reconcile(self.read_people())

    def reconcile(self, current):
        now = time.time()

        for tr in self.people_tracks:
            if tr["rid"] in current:
                tr["pos"] = current[tr["rid"]]
                tr["gone_at"] = None
            elif tr["gone_at"] is None:
                # start the confirmation window before calling it a departure
                tr["gone_at"] = now

        for rid in sorted(current):
            if any(
                tr["rid"] == rid and tr["gone_at"] is None for tr in self.people_tracks
            ):
                continue
            match = self._match_pending(rid, current[rid])
            if match is not None:
                # barely left the frame and came back a different id:
                # it's the same face, keep the same thread of conversation
                old_rid = match["rid"]
                match["rid"] = rid
                match["pos"] = current[rid]
                match["gone_at"] = None
                if old_rid in self.names:
                    self.names[rid] = self.names.pop(old_rid)
                print(
                    "Person id changed "
                    + str(old_rid)
                    + " -> "
                    + str(rid)
                    + " (same person, ignoring)"
                )
                continue
            self.people_tracks.append(
                {"rid": rid, "pos": current[rid], "gone_at": None}
            )
            self.on_human_arrived(rid)

        for tr in self.people_tracks:
            if tr["gone_at"] is not None and now - tr["gone_at"] > DEPARTURE_FORGET_S:
                self.on_human_left(tr["rid"])
                tr["gone_at"] = "gone"
        self.people_tracks = [
            tr for tr in self.people_tracks if tr["gone_at"] != "gone"
        ]

    def _match_pending(self, rid, pos):
        # find a recently-vanished person whose last spot this new id landed on
        now = time.time()
        best = None
        best_dist = CLOSE_DISTANCE_M
        for tr in self.people_tracks:
            if tr["gone_at"] is None:
                continue
            if now - tr["gone_at"] > DEPARTURE_FORGET_S:
                continue
            if pos is None or tr["pos"] is None:
                continue
            d = ((pos[0] - tr["pos"][0]) ** 2 + (pos[1] - tr["pos"][1]) ** 2) ** 0.5
            if d < best_dist:
                best, best_dist = tr, d
        return best

    def clear_files(self):
        with open(LISTEN_FILE, "w") as f:
            f.write("no")
        with open(RESPONSE_FILE, "w") as f:
            f.write(" ")

    def ask_listener_for_name(self, person_id):
        with open(RESPONSE_FILE, "w") as f:
            f.write(" ")
        with open(LISTEN_FILE, "w") as f:
            f.write("listen " + str(person_id))

        deadline = time.time() + NAME_TIMEOUT
        while time.time() < deadline:
            try:
                with open(RESPONSE_FILE, "r") as f:
                    text = f.read().strip()
            except Exception:
                text = ""

            if "|" in text:
                heard_id, name = text.split("|", 1)
                name = name.strip()
                if heard_id.strip() == str(person_id) and name != "":
                    with open(LISTEN_FILE, "w") as f:
                        f.write("no")
                    return name

            time.sleep(0.25)

        # gave up
        with open(LISTEN_FILE, "w") as f:
            f.write("no")
        return None

    def on_human_arrived(self, person_id):
        """
        Called by the people watcher when a new person id appears.
        """

        with self.pending_goodbyes_lock:
            pending = list(self.pending_goodbyes.items())
            self.pending_goodbyes.clear()
        if pending:
            for _, timer in pending:
                timer.cancel()
            print(
                "Someone came right back (was id "
                + str([p for p, _ in pending])
                + ", now id "
                + str(person_id)
                + "), skipping goodbye/re-greet"
            )
            return

        print("A human has arrived! id = " + str(person_id))

        t = threading.Thread(target=self.greet, args=(person_id,))
        self.greet_threads[person_id] = t
        t.start()

    def greet(self, person_id):
        if not self.talking.acquire(False):
            print("Already talking to someone, skipping id " + str(person_id))
            return
        try:
            print("Greeting person " + str(person_id))
            self.tts.say("Hi!")
            self.tts.say("What is your name?")

            print("Asking the listener for a name (person " + str(person_id) + ")")
            name = self.ask_listener_for_name(person_id)
            if name:
                self.names[person_id] = name
                print("Person " + str(person_id) + " is named " + name)
                self.tts.say("Nice to meet you, " + name)
            else:
                print("Never heard a name for person " + str(person_id))
                self.tts.say("Sorry, I did not catch that.")
        finally:
            self.talking.release()

    def on_human_left(self, person_id):
        print("A human has left! id = " + str(person_id))

        timer = threading.Timer(
            GOODBYE_GRACE_PERIOD, self._say_goodbye, args=(person_id,)
        )
        with self.pending_goodbyes_lock:
            self.pending_goodbyes[person_id] = timer
        timer.start()

    def _say_goodbye(self, person_id):
        with self.pending_goodbyes_lock:
            if person_id not in self.pending_goodbyes:
                return
            del self.pending_goodbyes[person_id]

        print("Confirmed left, saying goodbye to id = " + str(person_id))

        greet_thread = self.greet_threads.pop(person_id, None)
        if greet_thread and greet_thread.is_alive():
            greet_thread.join(timeout=NAME_TIMEOUT + 2)

        name = self.names.pop(person_id, None)
        if name:
            self.tts.say("Goodbye, " + name)
        else:
            self.tts.say("Goodbye!")

    def run(self):
        print("Starting HumanGreeter")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Interrupted by user, stopping HumanGreeter")
            self.shutdown()
            sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", type=str, default=ROBOT_IP, help="Robot IP address.")
    parser.add_argument("--port", type=int, default=9559, help="Naoqi port number")

    args = parser.parse_args()
    try:
        connection_url = "tcp://" + args.ip + ":" + str(args.port)
        app = qi.Application(["HumanGreeter", "--qi-url=" + connection_url])
    except RuntimeError:
        print(
            "Can't connect to Naoqi at ip \""
            + args.ip
            + '" on port '
            + str(args.port)
            + ".\n"
            "Please check your script arguments. Run with -h option for help."
        )
        sys.exit(1)

    human_greeter = None
    try:
        human_greeter = HumanGreeter(app)
        human_greeter.run()
    except KeyboardInterrupt:
        print("Interrupted by user, stopping HumanGreeter")
        if human_greeter is not None:
            human_greeter.shutdown()
        sys.exit(0)
