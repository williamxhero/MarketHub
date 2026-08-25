"""Streaming local artifact preparation; never publishes data."""
from __future__ import annotations
import argparse,csv,hashlib,json,os,re,uuid,shutil
from datetime import datetime,timedelta
from pathlib import Path
from typing import Mapping
import pyarrow as pa
import pyarrow.parquet as pq
from quotemux.futures import normalize_product_codes
from services.futures_partial_publication import (canonical_staged_row_bytes, classify_pyramid_timestamp_group, iter_pyramid_timestamp_groups, parse_pyramid_raw_fields, pyramid_timestamp)
BATCH_SIZE=100_000
NORMALIZED_SCHEMA=pa.schema([("product_code",pa.string()),("exchange",pa.string()),("bar_time",pa.string()),("open",pa.float64()),("high",pa.float64()),("low",pa.float64()),("close",pa.float64()),("volume",pa.float64()),("open_interest",pa.float64()),("adjustment_offset",pa.float64()),("source_key",pa.string())])
STAGED_SCHEMA=pa.schema([("product_code",pa.string()),("exchange",pa.string()),("raw_path",pa.string()),("source_line",pa.int64()),("bar_time",pa.string()),("open",pa.float64()),("high",pa.float64()),("low",pa.float64()),("close",pa.float64()),("volume",pa.float64()),("adjustment_offset",pa.float64()),("timestamp_group",pa.string()),("status",pa.string()),("reason",pa.string())])
def canonical_bytes(v:object)->bytes:return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def canonical_row_bytes(r:Mapping[str,object])->bytes:return canonical_bytes({n:r.get(n) for n in NORMALIZED_SCHEMA.names})+b"\n"
def sha(p:Path)->str:
 h=hashlib.sha256()
 with p.open("rb") as f:
  for b in iter(lambda:f.read(1024*1024),b""):h.update(b)
 return h.hexdigest()
def copy_and_hash(source:Path,target:Path)->tuple[int,str]:
 """Stream an exact local copy into the atomic bundle; never retain external paths."""
 target.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256();n=0
 with source.open("rb") as r,target.open("xb") as w:
  for b in iter(lambda:r.read(1024*1024),b""):
   w.write(b);h.update(b);n+=len(b)
 return n,h.hexdigest()
# Keep the migration's raw interpretation physically shared with the bundle
# validator; aliases preserve the compact CLI implementation below.
ts=pyramid_timestamp
parse=parse_pyramid_raw_fields
groups=iter_pyramid_timestamp_groups
def flush(w:pq.ParquetWriter,b:list[dict[str,object]],s:pa.Schema)->None:
 if b:w.write_table(pa.Table.from_pylist(b,schema=s));b.clear()
def verify(p:Path,s:pa.Schema,count:int)->None:
 with p.open("rb") as f:
  if f.read(4)!=b"PAR1":raise RuntimeError("parquet header invalid")
  f.seek(-4,os.SEEK_END)
  if f.read(4)!=b"PAR1":raise RuntimeError("parquet footer invalid")
 q=pq.ParquetFile(p)
 if q.schema_arrow!=s or q.metadata.num_rows!=count:raise RuntimeError("parquet schema/count mismatch")
 if any(str(q.metadata.row_group(i).column(j).compression).upper()!="SNAPPY" for i in range(q.metadata.num_row_groups) for j in range(q.metadata.row_group(i).num_columns)):raise RuntimeError("parquet must be SNAPPY")
def entry(p:str,e:str,a:datetime,b:datetime,status:str,h:str,detail:dict[str,object])->dict[str,object]:return {"product_code":p,"exchange":e,"start_time":a.isoformat(sep=" "),"end_time":b.isoformat(sep=" "),"status":status,"evidence_sha256":h,"detail":detail}
def interval_bytes(x:Mapping[str,object])->bytes:return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()+b"\n"
class IntervalSink:
 def __init__(self,path:Path):self.path=path;self.f=path.open("xb");self.h=hashlib.sha256();self.count=0;self.status={x:0 for x in ("accepted","excluded","residual")};self.products={}
 def append(self,x:dict[str,object],rawsha:str=""):
  if not x["evidence_sha256"]:x["evidence_sha256"]=rawsha if x["status"]=="accepted" else hashlib.sha256(canonical_bytes(x["detail"])).hexdigest()
  b=interval_bytes(x);self.f.write(b);self.h.update(b);self.count+=1;self.status[x["status"]]+=1;p=self.products.setdefault(x["product_code"],{s:0 for s in self.status});p[x["status"]]+=1
 def close(self):self.f.close();return {"path":"intervals.jsonl","logical_name":"intervals","size_bytes":self.path.stat().st_size,"sha256":sha(self.path),"rowset_sha256":self.h.hexdigest(),"row_count":self.count,"status_counts":self.status,"product_counts":self.products}
def sources(spec:Mapping[str,object])->list[dict[str,object]]:
 fs=spec.get("files")
 if not isinstance(fs,list) or not fs:raise ValueError("spec requires files")
 out=[];sp=set();sf=set()
 for x in fs:
  if not isinstance(x,dict):raise ValueError("file specification must be object")
  codes=normalize_product_codes([str(x.get("product_code",""))]);p=Path(str(x.get("raw_path",""))).resolve();e=str(x.get("exchange","")).upper().strip()
  if len(codes)!=1 or not p.is_file() or not e or codes[0] in sp or p in sf:raise ValueError(f"invalid spec product/path/exchange: {x.get('product_code')} {p}")
  sp.add(codes[0]);sf.add(p);out.append({"product":codes[0],"exchange":e,"path":p,"encoding":str(x.get("encoding","gbk")),"evidence":x.get("evidence_paths",[])})
 return sorted(out,key=lambda x:x["product"])
def preflight_spec(spec_path:Path,out:Path)->dict[str,object]:
 raw=spec_path.read_text(encoding="utf-8")
 spec=json.loads(raw)
 if not isinstance(spec,dict) or out.exists():raise ValueError("spec must be object and bundle output must be fresh")
 src=sources(spec);evidence=[]
 for item in src:
  if item["path"].stat().st_size<1:raise ValueError(f"empty raw file: {item['path']}")
  with item["path"].open("r",encoding=item["encoding"],newline="") as f:
   first=f.readline()
  if not first:raise ValueError(f"empty raw file: {item['path']}")
  parse(next(csv.reader([first],delimiter="\t")))
  for value in item["evidence"]:
   path=Path(str(value));
   if not path.is_file() or path.stat().st_size<1:raise ValueError(f"evidence missing or empty: {path}")
   evidence.append(str(path.resolve()))
 if not evidence:raise ValueError("spec requires evidence files")
 return {"status":"preflight_ok","products":[x["product"] for x in src],"raw_bytes":sum(x["path"].stat().st_size for x in src),"evidence_files":len(evidence),"bundle_root":str(out)}
def prepare_spec(spec_path:Path,out:Path,batch_size:int=BATCH_SIZE)->dict[str,object]:
 spec=json.loads(spec_path.read_text(encoding="utf-8"))
 if not isinstance(spec,dict):raise ValueError("spec must be object")
 if out.exists():raise ValueError("bundle output directory must be fresh and non-existing")
 src=sources(spec);end=ts(str(spec["declared_request_end"])) if spec.get("declared_request_end") else None
 final=out;out=out.with_name(out.name+".partial-"+uuid.uuid4().hex);out.mkdir(parents=True);stage=out/"staged.parquet";norm=out/"normalized.parquet";st=Path(str(stage)+".partial");nt=Path(str(norm)+".partial")
 raws=[];evidence=[];coverage={};sc=nc=0;rh=hashlib.sha256();stage_rowset=hashlib.sha256();interval_partial=out/"intervals.jsonl.partial";intervals=IntervalSink(interval_partial)
 try:
  with pq.ParquetWriter(st,STAGED_SCHEMA,compression="snappy") as sw,pq.ParquetWriter(nt,NORMALIZED_SCHEMA,compression="snappy") as nw:
   sb=[];nb=[]
   for s in src:
    p,e,path=s["product"],s["exchange"],s["path"];raw_name=f"raw/{p}{path.suffix.lower() or '.txt'}";raw_copy=out/raw_name;raw_size,rawsha=copy_and_hash(path,raw_copy);raws.append({"path":raw_name,"logical_name":f"{p}_source","size_bytes":raw_size,"sha256":rawsha,"product_code":p,"exchange":e,"encoding":s["encoding"]})
    for z in s["evidence"]:
     q=Path(str(z));
     if not q.is_file():raise ValueError(f"evidence missing: {q}")
     evidence_name=f"evidence/{p}/{len(evidence):03d}_{q.name}";evidence_copy=out/evidence_name;evidence_size,evidence_sha=copy_and_hash(q,evidence_copy)
     evidence.append({"path":evidence_name,"logical_name":f"{p}_evidence_{len(evidence):03d}","size_bytes":evidence_size,"sha256":evidence_sha})
    ast=aend=None;ac=0;prev=first=last=None;raw_rows=valid_rows=conflict_keys=conflict_rows=invalid_rows=0;before=dict(intervals.products.get(p,{s:0 for s in intervals.status}))
    for t,items,gh in groups(raw_copy,s["encoding"]):
     first=first or t;last=t;bad,reason=classify_pyramid_timestamp_group(items)
     raw_rows+=len(items)
     if len(items)!=1:conflict_keys+=1;conflict_rows+=len(items)
     elif items[0]["_status"]!="valid":invalid_rows+=1
     else:valid_rows+=1
     for x in items:
      staged_row={"product_code":p,"exchange":e,"raw_path":raw_name,"source_line":x["_source_line"],"bar_time":t.isoformat(sep=" "),**{n:x.get(n) for n in ("open","high","low","close","volume","adjustment_offset")},"timestamp_group":f"{p}|{t.isoformat(sep=' ')}","status":"excluded" if bad else "valid","reason":reason};sb.append(staged_row);stage_rowset.update(canonical_staged_row_bytes(staged_row));sc+=1
      if len(sb)>=batch_size:flush(sw,sb,STAGED_SCHEMA)
     if prev is not None and t>prev+timedelta(minutes=1):
      if ast is not None:intervals.append(entry(p,e,ast,aend,"accepted","",{"bar_count":ac,"contiguous":True}),rawsha);ast=None;ac=0
      intervals.append(entry(p,e,prev+timedelta(minutes=1),t-timedelta(minutes=1),"residual","",{"reason":"unclassified_no_observed_bar_wall_clock_gap","may_include_out_of_session":True}),rawsha)
     if bad:
      if ast is not None:intervals.append(entry(p,e,ast,aend,"accepted","",{"bar_count":ac,"contiguous":True}),rawsha);ast=None;ac=0
      intervals.append(entry(p,e,t,t,"excluded",gh,{"reason":reason,"source_lines":[x["_source_line"] for x in items]}),rawsha)
     else:
      r={"product_code":p,"exchange":e,"bar_time":t.isoformat(sep=" "),**{n:items[0][n] for n in ("open","high","low","close","volume","adjustment_offset")},"open_interest":None,"source_key":f"pyramid:{rawsha}"};nb.append(r);nc+=1;rh.update(canonical_row_bytes(r))
      if len(nb)>=batch_size:flush(nw,nb,NORMALIZED_SCHEMA)
      if ast is None:ast=t;ac=0
      aend=t;ac+=1
     prev=t
    if first is None:raise ValueError(f"empty raw file: {path}")
    if ast is not None:intervals.append(entry(p,e,ast,aend,"accepted","",{"bar_count":ac,"contiguous":True}),rawsha)
    if end and last<end:intervals.append(entry(p,e,last+timedelta(minutes=1),end,"residual","",{"reason":"declared_request_end_after_source"}),rawsha)
    counts=intervals.products.get(p,{s:0 for s in intervals.status})
    coverage[p]={"actual_start":first.isoformat(sep=" "),"actual_end":last.isoformat(sep=" "),"exchange":e,"raw_rows":raw_rows,"valid_rows":valid_rows,"conflicting_timestamp_keys":conflict_keys,"conflicting_rows_removed":conflict_rows,"invalid_ohlcv_rows":invalid_rows,"accepted_interval_count":counts["accepted"],"excluded_interval_count":counts["excluded"],"residual_interval_count":counts["residual"]}
   flush(sw,sb,STAGED_SCHEMA);flush(nw,nb,NORMALIZED_SCHEMA)
  verify(st,STAGED_SCHEMA,sc);verify(nt,NORMALIZED_SCHEMA,nc);st.replace(stage);nt.replace(norm);intervals_descriptor=intervals.close();interval_partial.replace(out/"intervals.jsonl")
 except Exception:
  intervals.f.close();st.unlink(missing_ok=True);nt.unlink(missing_ok=True);shutil.rmtree(out,ignore_errors=True);raise
 ss,ns=sha(stage),sha(norm)
 if not evidence:raise ValueError("spec requires evidence files")
 lin=dict(spec.get("source_lineage",{}));lin.update({"raw_artifact_sha256":hashlib.sha256(canonical_bytes(raws)).hexdigest(),"staged_artifact_sha256":ss,"staged_rowset_sha256":stage_rowset.hexdigest(),"normalized_artifact_sha256":ns,"normalized_rowset_sha256":rh.hexdigest(),"license":str(lin.get("license","retention_unverified")),"provider":str(lin.get("provider","local_user_provided")),"provider_package_version":str(lin.get("provider_package_version","unverified")),"timestamp_contract":str(lin.get("timestamp_contract","local-naive Asia/Shanghai assumption")),"adjustment":str(lin.get("adjustment","pyramid offset semantics unverified")),"roll_mapping":str(lin.get("roll_mapping","evidence supplied separately")),"oi_semantics":"unavailable","fields":"OHLCV,adjustment_offset; OI unavailable","catalog_version":str(lin.get("catalog_version","unverified")),"calendar_version":str(lin.get("calendar_version","unverified")),"session_contract":str(lin.get("session_contract","unverified")),"session_evidence_sha256":str(lin.get("session_evidence_sha256",evidence[0]["sha256"])),"timezone":"Asia/Shanghai assumed local-naive","bar_label":"unverified","units":"unknown","source_boundary":"local evidence; entitlement unverified","missing_bar_semantics":"excluded/residual skipped; never interpolated"})
 bundle_files=[*({"role":"raw",**x} for x in raws),*({"role":"evidence",**x} for x in evidence),{"role":"staged","path":"staged.parquet","logical_name":"staged","size_bytes":stage.stat().st_size,"sha256":ss},{"role":"normalized","path":"normalized.parquet","logical_name":"normalized","size_bytes":norm.stat().st_size,"sha256":ns},{"role":"intervals",**intervals_descriptor}]
 manifest={"schema_version":"futures_pyramid_partial_bundle_v5","dataset_id":spec["dataset_id"],"source_id":spec["source_id"],"read_series_type":spec.get("read_series_type","back_adjusted_continuous"),"source_series_state":{"kind":"artifact_bundle","generation_id":hashlib.sha256(canonical_bytes({"raw":raws,"evidence":evidence,"normalized":ns})).hexdigest(),"row_count":nc,"first_bar_time":min(x["actual_start"] for x in coverage.values()),"last_bar_time":max(x["actual_end"] for x in coverage.values())},"source_lineage":lin,"authorization":spec.get("authorization",{}),"normalized_row_count":nc,"product_coverage":coverage,"interval_artifact":intervals_descriptor,"artifact_bundle":{"files":bundle_files,"raw_files":raws,"evidence_files":evidence,"staged_artifact_sha256":ss,"normalized_artifact_sha256":ns}}
 mp=out/"manifest.json";tmp=Path(str(mp)+".partial");tmp.write_bytes(canonical_bytes(manifest));tmp.replace(mp)
 for path in (stage,norm,mp):
  fd=os.open(path,os.O_RDONLY)
  try:
   try:os.fsync(fd)
   except OSError:pass  # Windows does not permit fsync on a read-only handle.
  finally:os.close(fd)
 out.replace(final)
 return {"staged":str(final/"staged.parquet"),"normalized":str(final/"normalized.parquet"),"manifest":str(final/"manifest.json"),"rows":nc}
def main()->int:
 p=argparse.ArgumentParser();p.add_argument("--spec",type=Path,required=True);p.add_argument("--out",type=Path,required=True);p.add_argument("--batch-size",type=int,default=BATCH_SIZE);p.add_argument("--preflight-only",action="store_true");a=p.parse_args()
 if a.batch_size<1:raise SystemExit("batch-size must be positive")
 print(json.dumps(preflight_spec(a.spec,a.out) if a.preflight_only else prepare_spec(a.spec,a.out,a.batch_size),ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
