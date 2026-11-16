import random
from freshness.detector import compare_html

TOPICS = ["city council", "school board", "football team", "weather service", "museum", "hospital", "university", "train company", "library", "power plant"]


def make_page(paragraphs, headline, stamp="Nov 3, 2026 10:40 am", script="var x=1;", views="", ad=""):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f"<html><head><title>{headline}</title><script>{script}</script>"
            f"<style>.a{{color:red}}</style></head><body><nav>Home | News | Sports</nav>"
            f"<article><h1>{headline}</h1><p class='byline'>Updated {stamp}</p>{body}{views}</article>"
            f"<aside>{ad}</aside><footer>© 2026 Example News</footer></body></html>")


def base_article(rng, topic):
    words = ("the {t} said on {d} that the new plan will change how people in the region work, travel and "
             "pay for basic services, and officials expect the first results to appear within several months "
             "after a long public discussion with local groups").split()
    paras = []
    for i in range(10):
        w = words[:]
        rng.shuffle(w)
        paras.append(" ".join(w).replace("{t}", topic).replace("{d}", "monday") + f" point {i}.")
    return paras


def noise_edit(i, paras, headline):
    kinds = [
        dict(stamp="Nov 4, 2026 11:15 pm"),
        dict(stamp="2026-11-04T23:15:00Z"),
        dict(script="var x=2; trackView(98213);"),
        dict(stamp="5 minutes ago"),
        dict(stamp="Nov 3, 2026 10:41 am", script="var cacheBust='a8f3';"),
    ]
    return make_page(paras, headline, **kinds[i % len(kinds)])


def real_edit(i, paras, headline, rng):
    """Edits a reader would care about."""
    p = list(paras)
    k = i % 4
    if k == 0:  
        p.insert(3, "breaking update the minister resigned this evening after investigators found missing "
                    "funds and the opposition demanded a full inquiry into the budget decisions of the last year")
    elif k == 1:  
        del p[2]
    elif k == 2:  
        p[0] = "correction an earlier version of this story gave the wrong numbers for the project costs " \
               "which are now estimated at twice the original amount according to new documents"
        p[5] = "residents who spoke to reporters said they were surprised by the decision and many plan " \
               "to attend the next meeting to ask questions about the timeline and the price"
    else:        
        headline = "Plan cancelled after vote"
        p[0] = "the plan was cancelled late on friday after members voted against it by a large margin " \
               "ending months of debate and leaving the future of the services unclear for everyone"
    return make_page(p, headline)


def test_ct3_noise_vs_real():
    rng = random.Random(42)
    correct, results = 0, []
    for i in range(20):
        topic = TOPICS[i % len(TOPICS)]
        paras = base_article(rng, topic)
        headline = f"New plan from the {topic}"
        before = make_page(paras, headline)
        sim, meaningful = compare_html(before, noise_edit(i, paras, headline))
        results.append(("noise", i, sim, meaningful))
        correct += int(not meaningful)
        sim, meaningful = compare_html(before, real_edit(i, paras, headline, rng))
        results.append(("real", i, sim, meaningful))
        correct += int(meaningful)
    accuracy = correct / 40
    print("\n".join(f"{k:5s} {i:2d} sim={s:.3f} meaningful={m}" for k, i, s, m in results))
    assert accuracy >= 0.90, f"accuracy {accuracy:.0%}"


def test_identical_pages_not_meaningful():
    page = make_page(["same text " * 30], "Same")
    assert compare_html(page, page) == (1.0, False)
