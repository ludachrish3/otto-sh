/* Start each termynal block when it scrolls into view.
 *
 * The vendored termynal.js only auto-initialises containers named on its own
 * <script> tag; the docs instead animate every `[data-termynal]` lazily so a
 * page full of terminals doesn't play all at once (and respects
 * prefers-reduced-motion by skipping the typing animation). */

'use strict';

document.addEventListener('DOMContentLoaded', () => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const options = reduced
        ? { startDelay: 0, typeDelay: 1, lineDelay: 1 }
        : {};
    const containers = document.querySelectorAll('[data-termynal]');
    if (!('IntersectionObserver' in window)) {
        containers.forEach((el) => new Termynal(el, options));
        return;
    }
    const observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            observer.unobserve(entry.target);
            new Termynal(entry.target, options);
        }
    }, { threshold: 0.2 });
    containers.forEach((el) => observer.observe(el));
});
