// Filter chip handling: shows/hides .failure-card elements by data-type.
// Runs once the page loads; independent of openLogTab below.
document.addEventListener('DOMContentLoaded', function () {
    var chips = document.querySelectorAll('.chip');
    var cards = document.querySelectorAll('.failure-card');

    chips.forEach(function (chip) {
        chip.addEventListener('click', function () {
            chips.forEach(function (c) { c.classList.remove('active'); });
            chip.classList.add('active');

            var type = chip.getAttribute('data-type');
            cards.forEach(function (card) {
                var show = (type === 'all' || card.getAttribute('data-type') === type);
                card.classList.toggle('hidden', !show);
            });
        });
    });
});

function openLogTab(url) {
    var tab = window.open('', 'rf_log_viewer');
    if (tab) {
        var parts = url.split('#');
        var base = parts[0];
        var hash = parts[1] ? parts[1] : '';
        var separator = base.indexOf('?') !== -1 ? '&' : '?';
        
        // Cache-buster forces log.html to evaluate fresh parameters even on repetitive clicks
        tab.location.href = base + separator + 'cb=' + new Date().getTime() + (hash ? '#' + hash : '');
        
        if (hash) {
            var checks = 0;
            var interval = setInterval(function() {
                checks++;
                if (tab.util && tab.util.expandElementWithId) {
                    clearInterval(interval);
                    try {
                        // Direct call to RF internal framework to force full ancestral layout tree expansion
                        tab.util.expandElementWithId(hash);
                        var element = tab.document.getElementById(hash);
                        if (element) {
                            element.scrollIntoView({ block: "center", behavior: "smooth" });
                        }
                    } catch(e) {
                        console.log("Deep expand injection waiting...", e);
                    }
                }
                if (checks > 50) clearInterval(interval);
            }, 100);
        }
        tab.focus();
    }
}