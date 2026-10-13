from radar.config import load_config
from radar.extract import extract, normalize_name
from radar import security
from tests.conftest import make_post

CFG = load_config(load_env_file=False, environ={})
NETS = CFG.get("networks")
SEC = CFG.get("security")


def ex(text, **kw):
    return extract(make_post(text, **kw), NETS, SEC, set(), {"opensea", "magiceden", "base"})


def test_irrelevant_post():
    assert not ex("free coffee today").relevant
    assert not ex("NFT mint at 0.05 ETH").relevant


def test_base_beats_eth_keyword():
    e = ex("Moon Toads free mint on Base, 0 ETH")
    assert e.relevant and e.chain == "base"


def test_solana_and_ordinals():
    assert ex("free mint on Solana tomorrow").chain == "solana"
    assert ex("Ordinals free mint, inscribe now").chain == "bitcoin_ordinals"


def test_supply_eligibility_links():
    e = ex("Moon Toads free mint! Supply: 5,555. Allowlist + FCFS public. https://moontoads.xyz/mint")
    assert e.supply == 5555
    assert "allowlist" in e.eligibility and "first-come-first-served" in e.eligibility
    assert e.mint_url == "https://moontoads.xyz/mint" and e.website_url == "https://moontoads.xyz"


def test_known_platform_mint_link():
    e = ex("free mint live https://magiceden.io/launchpad/moon_toads", urls=["https://magiceden.io/launchpad/moon_toads"])
    assert e.mint_platform == "Magic Eden"


def test_paid_price_flagged():
    e = ex("Free mint for WL, public 0.01 ETH")
    assert e.paid_price == "0.01 ETH"
    assert any("non-zero price" in f for f in e.risk_flags)


def test_never_invents_mint_url():
    e = ex("Moon Toads free mint soon, details later")
    assert e.mint_url is None and e.website_url is None


def test_project_handle_from_aggregator_mention():
    e = ex("Don't miss @moontoads Moon Toads free mint", author="alphacaller")
    assert e.project_handle.lower() == "moontoads" and not e.author_is_project


def test_url_flags():
    flags = security.url_flags("https://opensea-claim.top/free", SEC)
    assert any("not the official opensea" in f for f in flags)
    assert any(".top" in f for f in flags)
    assert any("shortened" in f for f in security.url_flags("https://bit.ly/abc", SEC))
    assert any("punycode" in f for f in security.url_flags("https://xn--opnsea-abc.io", SEC))
    assert any("look-alike" in f or "typosquat" in f for f in security.url_flags("https://0pensea.io/x", SEC))
    assert security.url_flags("https://opensea.io/collection/x", SEC) == []


def test_text_flags_drainer_wording():
    flags = security.text_flags("Enter your seed phrase to validate your wallet")
    assert len(flags) >= 2 and all(security.is_high_risk(f) for f in flags[:1])


def test_impersonation_flag():
    flags = security.handle_flags("moontoads_mint", "moontoads", 50, 100, False)
    assert any("impersonation" in f for f in flags) and any("followers" in f for f in flags)


def test_normalize_name():
    assert normalize_name("The Moon Toads NFT") == normalize_name("moon-toads") == "moontoads"
