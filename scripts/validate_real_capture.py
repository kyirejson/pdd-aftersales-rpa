from __future__ import annotations

import json

from pdd_rpa.config import load_config
from pdd_rpa.connectors.pdd_edge_readonly import WindowsEdgeReadOnlySession, select_shop_snapshot
from pdd_rpa.element_capture import ElementCaptureService


DISPATCH_IMMEDIATE_PROCESS = r"""
(()=>{
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
  const candidates=[...document.querySelectorAll('button,a,div,span')].filter(e=>(e.innerText||'').trim()==='立即处理'&&visible(e));
  candidates.sort((a,b)=>a.querySelectorAll('*').length-b.querySelectorAll('*').length);
  const target=candidates[0];
  if(!target){window.__PDD_RPA_CAPTURE_STATE={status:'not_found',result:null};return;}
  for(const type of ['pointerdown','mousedown','mouseup','click'])target.dispatchEvent(new MouseEvent(type,{bubbles:true,cancelable:true,view:window}));
  void 0;
})()
""".strip()


def main() -> None:
    config = load_config()
    service = ElementCaptureService(config.raw["pdd_readonly"], config.shops)
    results = []
    for configured_shop in config.shops:
        shop = {
            "id": configured_shop.id,
            "name": configured_shop.name,
            "edge_profile": configured_shop.edge_profile,
        }
        session = WindowsEdgeReadOnlySession(config.raw["pdd_readonly"])
        before = select_shop_snapshot(session.snapshots(), shop)
        service.start(configured_shop.id)
        session.execute_script(before.handle, DISPATCH_IMMEDIATE_PROCESS)
        captured = service.status(configured_shop.id)
        after = select_shop_snapshot(session.snapshots(), shop)
        results.append(
            {
                "shop_id": configured_shop.id,
                "shop_name": configured_shop.name,
                "profile": configured_shop.edge_profile,
                "status": captured.get("status"),
                "text": (captured.get("result") or {}).get("text"),
                "selector": (captured.get("result") or {}).get("selector"),
                "selector_unique": (captured.get("result") or {}).get("selector_unique"),
                "url_unchanged": before.url == after.url,
                "url": after.url,
            }
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
