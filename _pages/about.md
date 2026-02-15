---
layout: single
title: About
permalink: /
author_profile: true
description: "Software engineer at Amazon focused on distributed systems, backend architecture, and technical writing."
---

I am a Software Development Engineer at Amazon in London, focused on backend and distributed systems.

I completed a Master of Science (by Research) in Computer Science and Engineering at the [Indian Institute of Technology Kanpur](http://www.iitk.ac.in/){:target="\_blank"}, under the supervision of [Prof. Indranil Saha](https://www.cse.iitk.ac.in/users/isaha/){:target="\_blank"}. I also hold a Bachelor of Technology in Computer Science and Engineering from Kamla Nehru Institute of Technology, Sultanpur.

Before graduate school, I worked with Microsoft, Adobe, IFFCO, and Alcatel-Lucent (now Nokia). In Summer 2019, I was a Visiting Research Scholar at VERIMAG Labs, Grenoble, France, where I worked under [Prof. Thao Dang](http://www-verimag.imag.fr/PEOPLE/Thao.Dang/){:target="\_blank"}.

## Recent Writing

{% assign recent_posts = site.posts | sort: "date" | reverse | slice: 0, 5 %}
{% for post in recent_posts %}
- [{{ post.title }}]({{ post.url | relative_url }}){% if post.categories and post.categories.size > 0 %} · {{ post.categories | join: ", " }}{% endif %}
{% endfor %}
