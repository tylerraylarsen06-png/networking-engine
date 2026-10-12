# The networking message method

This is the method `outreach.py` follows when it writes a networking message. The checker in
`method.py` enforces every hard rule below, and `test_method.py` holds one failing example per rule.

## The idea

You are not a curious student asking someone to explain their job. You are someone doing similar
work who wants advice from a person who is better at it. People like talking about their
specialty, so the message aims at it.

## Shape

One flowing message, three to four sentences, under about 90 words.

1. Intro: "I'm a [school] [year] studying [major]."
2. Peer link: What you actually do that matches their specialty, then what they do, then why
   it interests you. Example: "I run an AI club that does AI marketing for companies, and you're
   using AI to market audiobooks at a huge scale, which is really interesting to me."
3. Ask: "I'd love any tips you have for someone [doing that same work] like me." One ask only.
4. Close: "Would you have 15 minutes in the next couple weeks?"

The message opens with "Hi [first name]," and signs off with "Thanks," and your first name on the
next line.

## When you have no matching work

Fall back to an aspiration, and only one that is true for you:

"I'm hoping to start working with [industry] clients, and you're at one of the top [industry]
companies. I'd love any advice for someone trying to break in."

If the profile has no club, job or aspiration that honestly fits the person, the tool skips them.
It does not stretch a hook to fit.

## Truth

Every fact about you comes from `profile.yaml`, written by you. The tool never invents a club, a
job, an award or a number. When Claude helps write a message, it only writes the clause about the
recipient's work, from their title and public profile text. The sentences about you stay in the
template.

Notable facts, such as a sports moment, carry a `fits` list. A fact only appears for a recipient
whose industry, company or city is on that list.

## Hard rules

| Rule | Why |
| --- | --- |
| Opens "Hi [first name]," | A real greeting, with their name |
| Signs off "Thanks," then your first name | Short and normal |
| Three or four sentences | Long notes get skimmed, then archived |
| Under about 90 words in the body | Same reason |
| No em dashes, en dashes, or " - " | They read as generated text |
| Never "rather", "I saw", "I found your profile", "day to day" | Stock phrases that make it read like a template |
| No numbers about you | GPA, club size and follower counts read as a resume |
| Exactly one question mark | One ask, and it is the close |
| Never recite their career path | "After X and Y" or "you went from X to Y" tells them their own resume |
| Never name another company you are targeting | The message is about their company only |
