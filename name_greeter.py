#! /usr/bin/env python
# -*- encoding: UTF-8 -*-

import qi
import time
import os
import sys
import threading
import argparse

ROBOT_IP = "10.42.0.109"

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN_FILE = os.path.join(HERE, "listen.txt")
RESPONSE_FILE = os.path.join(HERE, "response.txt")

# each poll is a few getData calls per tracked person, so keep it coarse
NAME_TIMEOUT = 20.0
POLL_INTERVAL = 1.0

MAX_DETECTION_RANGE_M = 5.0
TIME_BEFORE_VISIBLE_S = 1.5
TIME_BEFORE_GONE_S = 3.0

# goodbye ~2s out
DEPARTURE_FORGET_S = 1.5
GOODBYE_GRACE_PERIOD = 0.5
QUEUE_DROP_S = 4.0

MAX_ASSOC_DISTANCE_M = 0.7
ASSOC_HEIGHT_TOLERANCE_M = 0.3


class HumanGreeter(object):
    def __init__(self, app):
        super(HumanGreeter, self).__init__()
        # reset the handshake first
        self.clear_files()
        app.start()
        session = app.session

        self.memory = session.service("ALMemory")

        # mac client libqi 2.8 can't subscribe to events on this 2.5.7 robot
        self.tts = session.service("ALTextToSpeech")
        self.motion = session.service("ALMotion")
        self.robot_posture = session.service("ALRobotPosture")
        self.awareness = session.service("ALBasicAwareness")
        try:
            self.people_perception = session.service("ALPeoplePerception")
        except Exception:
            self.people_perception = None

        try:
            self.stand_up()
            self.tune_people_perception()

            self.awareness.setEngagementMode("SemiEngaged")
            self.awareness.setTrackingMode("Head")
            self.awareness.startAwareness()
            print("Awareness on: SemiEngaged, Head tracking")

            self.greeted = {}
            self.greet_queue = []
            self.greet_event = threading.Event()
            self.state_lock = threading.Lock()
            # one voice at a time, or crowd talks over itself
            self.talking = threading.Lock()
            self._people_read_ok = True
            self._next_key = 1

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

    def tune_people_perception(self):
        pp = self.people_perception
        if pp is None:
            print("WARNING: ALPeoplePerception unavailable, using defaults")
            return

        for setter, value in (
            ("setFastModeEnabled", False),
            ("setMovementDetectionEnabled", True),
            ("setMaximumDetectionRange", MAX_DETECTION_RANGE_M),
            ("setTimeBeforeVisiblePersonDisappears", TIME_BEFORE_VISIBLE_S),
            ("setTimeBeforePersonDisappears", TIME_BEFORE_GONE_S),
        ):
            try:
                getattr(pp, setter)(value)
            except Exception as e:
                print("WARNING: " + setter + "(" + str(value) + ") failed: " + str(e))

        try:
            pp.resetPopulation()
        except Exception as e:
            print("WARNING: resetPopulation failed: " + str(e))

        for getter in (
            "getMaximumDetectionRange",
            "getTimeBeforePersonDisappears",
            "getTimeBeforeVisiblePersonDisappears",
            "isFastModeEnabled",
            "isMovementDetectionEnabled",
        ):
            try:
                print("  " + getter + " = " + str(getattr(pp, getter)()))
            except Exception as e:
                print("  " + getter + " unavailable: " + str(e))

    def shutdown(self):
        # safe to call mid-__init__ too; ctrl-c must always end in crouch
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
        try:
            ids = self.memory.getData("PeoplePerception/PeopleList")
            self._people_read_ok = True
        except Exception as e:
            if self._people_read_ok:
                print("WARNING: could not read PeoplePerception/PeopleList: " + str(e))
            self._people_read_ok = False
            return {}
        if not ids:
            return {}

        people = {}
        for rid in ids:
            rid = int(rid)
            people[rid] = {
                "visible": None,
                "not_seen": None,
                "pos": None,
                "height": None,
            }
            for field, key in (
                ("visible", "IsVisible"),
                ("not_seen", "NotSeenSince"),
                ("pos", "PositionInRobotFrame"),
                ("height", "RealHeight"),
            ):
                try:
                    value = self.memory.getData(
                        "PeoplePerception/Person/" + str(rid) + "/" + key
                    )
                except Exception:
                    continue
                if field == "visible":
                    people[rid][field] = bool(value)
                elif field == "pos":
                    try:
                        people[rid][field] = [float(v) for v in value]
                    except Exception:
                        people[rid][field] = None
                else:
                    try:
                        people[rid][field] = float(value)
                    except Exception:
                        people[rid][field] = None
        return people

    def start_people_watcher(self):
        self.stop_watching = threading.Event()
        snapshot = self.read_people()
        self.people_tracks = []
        for rid in sorted(snapshot):
            self.people_tracks.append(self._new_track(rid, snapshot[rid]))
        present = sorted(snapshot)
        print(
            "Watching for arrivals/departures. People currently present: "
            + str(present)
        )
        if present:
            # follow the track objects
            init = list(self.people_tracks)
            init_greet = threading.Thread(target=self._greet_existing, args=(init,))
            init_greet.daemon = True
            init_greet.start()
        t = threading.Thread(target=self.watch_people)
        t.daemon = True
        t.start()
        self.start_greet_worker()

    def _new_track(self, rid, info):
        self._next_key += 1
        return {
            "key": self._next_key,
            "rid": rid,
            "pos": (info or {}).get("pos"),
            "height": (info or {}).get("height"),
            "gone_at": None,
        }

    def _greet_existing(self, init_tracks):
        time.sleep(2.0)
        greeted = []
        for tr in init_tracks:
            if tr["gone_at"] is None and tr not in greeted:
                greeted.append(tr)
                self.on_human_arrived(tr)

    def watch_people(self):
        while not self.stop_watching.is_set():
            time.sleep(POLL_INTERVAL)
            self.reconcile(self.read_people())

    def reconcile(self, current):
        now = time.time()

        for tr in self.people_tracks:
            info = current.get(tr["rid"])
            if info is not None:
                if info.get("pos"):
                    tr["pos"] = info["pos"]
                if info.get("height") is not None:
                    tr["height"] = info["height"]
                # start the departure clock from IsVisible rather than waiting
                if info.get("visible") is False or (
                    info.get("not_seen") is not None and info["not_seen"] > 0
                ):
                    if tr["gone_at"] is None:
                        tr["gone_at"] = now
                else:
                    tr["gone_at"] = None
            elif tr["gone_at"] is None:
                tr["gone_at"] = now

        matched = set()
        for rid in sorted(current):
            if any(tr["rid"] == rid for tr in self.people_tracks):
                continue
            match = self._match_pending(rid, current[rid], matched)
            if match is not None:
                matched.add(id(match))
                old_rid = match["rid"]
                match["rid"] = rid
                info = current[rid]
                if info.get("pos"):
                    match["pos"] = info["pos"]
                if info.get("height") is not None:
                    match["height"] = info["height"]
                match["gone_at"] = None
                print(
                    "Person id changed "
                    + str(old_rid)
                    + " -> "
                    + str(rid)
                    + " (same person, ignoring)"
                )
                continue
            tr = self._new_track(rid, current[rid])
            self.people_tracks.append(tr)
            self.on_human_arrived(tr)

        for tr in self.people_tracks:
            if tr["gone_at"] is not None and now - tr["gone_at"] > DEPARTURE_FORGET_S:
                self.on_human_left(tr)
                tr["gone_at"] = "gone"
        self.people_tracks = [
            tr for tr in self.people_tracks if tr["gone_at"] != "gone"
        ]

    def _match_pending(self, rid, info, matched):
        # one-to-one, so a departing person can't be absorbed
        if info is None or not info.get("pos"):
            return None
        pos = info["pos"]
        height = info.get("height")
        now = time.time()
        best = None
        best_dist = MAX_ASSOC_DISTANCE_M
        for tr in self.people_tracks:
            if tr["gone_at"] is None or id(tr) in matched:
                continue
            if now - tr["gone_at"] > DEPARTURE_FORGET_S:
                continue
            if not tr["pos"]:
                continue
            if (
                height is not None
                and tr["height"] is not None
                and abs(height - tr["height"]) > ASSOC_HEIGHT_TOLERANCE_M
            ):
                continue
            d = (
                (pos[0] - tr["pos"][0]) ** 2
                + (pos[1] - tr["pos"][1]) ** 2
                + (pos[2] - tr["pos"][2]) ** 2
            ) ** 0.5
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

        with open(LISTEN_FILE, "w") as f:
            f.write("no")
        return None

    def on_human_arrived(self, tr):
        print("A human has arrived! id = " + str(tr["rid"]))
        with self.state_lock:
            if tr["key"] in self.greeted:
                return
            if not any(t is tr for t in self.greet_queue):
                self.greet_queue.append(tr)
        self.greet_event.set()

    def start_greet_worker(self):
        t = threading.Thread(target=self.greet_worker)
        t.daemon = True
        t.start()

    def _distance(self, tr):
        if not tr["pos"]:
            return 1e9
        return (tr["pos"][0] ** 2 + tr["pos"][1] ** 2) ** 0.5

    def _is_worth_greeting(self, tr):
        if tr["gone_at"] is None:
            return True
        return (time.time() - tr["gone_at"]) <= QUEUE_DROP_S

    def _pop_nearest(self):
        with self.state_lock:
            for t in list(self.greet_queue):
                if not self._is_worth_greeting(t):
                    self.greet_queue.remove(t)
            if not self.greet_queue:
                return None
            nearest = min(self.greet_queue, key=self._distance)
            self.greet_queue.remove(nearest)
            return nearest

    def greet_worker(self):
        while not self.stop_watching.is_set():
            self.greet_event.wait(0.5)
            self.greet_event.clear()
            while True:
                tr = self._pop_nearest()
                if tr is None:
                    break
                if not self._is_worth_greeting(tr):
                    continue
                self.greet(tr)

    def greet(self, tr):
        person_id = str(tr["rid"])
        with self.state_lock:
            if tr["key"] in self.greeted:
                return
            self.greeted[tr["key"]] = None
        print("Greeting person " + person_id)
        with self.talking:
            self.tts.say("Hi!")
            self.tts.say("What is your name?")

            print("Asking the listener for a name (person " + person_id + ")")
            name = self.ask_listener_for_name(person_id)
            if name:
                with self.state_lock:
                    self.greeted[tr["key"]] = name
                print("Person " + person_id + " is named " + name)
                self.tts.say("Nice to meet you, " + name)
            else:
                print("Never heard a name for person " + person_id)
                self.tts.say("Sorry, I did not catch that.")

    def on_human_left(self, tr):
        print("A human has left! id = " + str(tr["rid"]))
        with self.state_lock:
            greeted = tr["key"] in self.greeted
        # people we never got to greet leave quietly
        if not greeted:
            return
        timer = threading.Timer(GOODBYE_GRACE_PERIOD, self._say_goodbye, args=(tr,))
        timer.daemon = True
        timer.start()

    def _say_goodbye(self, tr):
        with self.state_lock:
            name = self.greeted.pop(tr["key"], False)
        if name is False:
            return
        print("Confirmed left, saying goodbye to id = " + str(tr["rid"]))
        with self.talking:
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
