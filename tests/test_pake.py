"""SPAKE2 against the RFC 9382 Appendix B test vectors, plus protocol properties."""

import pytest

from openitrack import pake

H = bytes.fromhex


def test_m_and_n_decode_to_curve_points():
    assert pake.M is not None and pake.N is not None


@pytest.mark.parametrize("id_a,id_b,w,x,y,pa,pb,k,ke,kca,kcb,ca,cb", [
    (b"server", b"client",
     0x2ee57912099d31560b3a44b1184b9b4866e904c49d12ac5042c97dca461b1a5f,
     0x43dd0fd7215bdcb482879fca3220c6a968e66d70b1356cac18bb26c84a78d729,
     0xdcb60106f276b02606d8ef0a328c02e4b629f84f89786af5befb0bc75b6e66be,
     "04a56fa807caaa53a4d28dbb9853b9815c61a411118a6fe516a8798434751470"
     "f9010153ac33d0d5f2047ffdb1a3e42c9b4e6be662766e1eeb4116988ede5f912c",
     "0406557e482bd03097ad0cbaa5df82115460d951e3451962f1eaf4367a420676"
     "d09857ccbc522686c83d1852abfa8ed6e4a1155cf8f1543ceca528afb591a1e0b7",
     "0412af7e89717850671913e6b469ace67bd90a4df8ce45c2af19010175e37eed"
     "69f75897996d539356e2fa6a406d528501f907e04d97515fbe83db277b715d3325",
     "0e0672dc86f8e45565d338b0540abe69", "00c12546835755c86d8c0db7851ae86f",
     "a9fa3406c3b781b93d804485430ca27a",
     "58ad4aa88e0b60d5061eb6b5dd93e80d9c4f00d127c65b3b35b1b5281fee38f0",
     "d3e2e547f1ae04f2dbdbf0fc4b79f8ecff2dff314b5d32fe9fcef2fb26dc459b"),
    (b"", b"client",
     0x0548d8729f730589e579b0475a582c1608138ddf7054b73b5381c7e883e2efae,
     0x403abbe3b1b4b9ba17e3032849759d723939a27a27b9d921c500edde18ed654b,
     0x903023b6598908936ea7c929bd761af6039577a9c3f9581064187c3049d87065,
     "04a897b769e681c62ac1c2357319a3d363f610839c4477720d24cbe32f5fd8"
     "5f44fb92ba966578c1b712be6962498834078262caa5b441ecfa9d4a9485720e918a",
     "04e0f816fd1c35e22065d5556215c097e799390d16661c386e0ecc84593974"
     "a61b881a8c82327687d0501862970c64565560cb5671f696048050ca66ca5f8cc7fc",
     "048f83ec9f6e4f87cc6f9dc740bdc2769725f923364f01c84148c049a39a735e"
     "bda82eac03e00112fd6a5710682767cff5361f7e819e53d8d3c3a2922e0d837aa6",
     "642f05c473c2cd79909f9a841e2f30a7", "c6be376fc7cd1301fd0a13adf3e7bffd",
     "b7243f4ae60440a49b3f8cab3c1fba07",
     "47d29e6666af1b7dd450d571233085d7a9866e4d49d2645e2df975489521232b",
     "3313c5cefc361d27fb16847a91c2a73b766ffa90a4839122a9b70a2f6bd1d6df"),
])
def test_rfc9382_vectors(id_a, id_b, w, x, y, pa, pb, k, ke, kca, kcb, ca, cb):
    a = pake.PartyA("", x=x, w=w, id_a=id_a, id_b=id_b, aad=b"")
    assert a.message == H(pa)
    pb_bytes, kb = pake.respond_with_scalar(w, a.message, y=y, id_a=id_a, id_b=id_b, aad=b"")
    assert pb_bytes == H(pb)
    ka = a.finish(pb_bytes)
    assert ka.tt == kb.tt and H(k) in ka.tt
    assert ka.ke == kb.ke == H(ke)
    assert ka.kc_a == H(kca) and ka.kc_b == H(kcb)
    assert ka.confirm_a() == H(ca) and kb.confirm_b() == H(cb)


def test_same_code_agrees_and_wrong_code_fails():
    code = pake.new_code()
    a = pake.PartyA(code)
    pb, kb = pake.respond_as_b(code.lower(), a.message)  # case-insensitive entry
    assert a.finish(pb).ke == kb.ke
    wrong = "".join("A" if c != "A" else "B" for c in code)
    a2 = pake.PartyA(code)
    pb2, kb2 = pake.respond_as_b(wrong, a2.message)
    assert a2.finish(pb2).confirm_b() != kb2.confirm_b()


def test_invalid_points_are_rejected():
    with pytest.raises(ValueError):
        pake.decode_point(b"\x04" + b"\x00" * 64)
    with pytest.raises(ValueError):
        pake.decode_point(b"\x05" + b"\x01" * 64)
    with pytest.raises(ValueError):
        pake.respond_as_b("ABCDEF", b"junk")


def test_codes():
    for _ in range(200):
        c = pake.new_code()
        assert pake.is_valid_code(c)
    assert pake.is_valid_code("AB2-3CD")
    assert not pake.is_valid_code("AB0CDE")   # 0 is excluded
    assert not pake.is_valid_code("ABCDE")
