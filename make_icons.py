"""Draws the home-screen icon (blue tile, white snowflake) as PNGs with no extra libraries."""
import math, struct, zlib, sys
def png(path, n):
    rows=[]
    c=n/2; L=n*0.34; w=n*0.045
    segs=[]
    for k in range(3):
        a=math.pi/3*k; dx,dy=math.cos(a),math.sin(a)
        segs.append(((c-dx*L,c-dy*L),(c+dx*L,c+dy*L)))
        for s in (1,-1):  # little branches near each tip
            for f in (0.62,):
                px,py=c+s*dx*L*f,c+s*dy*L*f
                for b in (math.pi/4,-math.pi/4):
                    bx,by=math.cos(a+b)*s,math.sin(a+b)*s
                    segs.append(((px,py),(px+bx*L*0.3,py+by*L*0.3)))
    def dseg(x,y,p,q):
        (x1,y1),(x2,y2)=p,q; vx,vy=x2-x1,y2-y1
        t=max(0,min(1,((x-x1)*vx+(y-y1)*vy)/(vx*vx+vy*vy)))
        return math.hypot(x-x1-t*vx,y-y1-t*vy)
    for y in range(n):
        r=bytearray([0])
        for x in range(n):
            g=y/n; R,G,B=int(14+(37-14)*g),int(165+(99-165)*g),int(233+(235-233)*g)
            d=min(dseg(x+.5,y+.5,p,q) for p,q in segs)
            a=max(0,min(1,w-d+.5))
            r+=bytes((int(R+(255-R)*a),int(G+(255-G)*a),int(B+(255-B)*a)))
        rows.append(bytes(r))
    raw=b"".join(rows)
    def ch(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
    open(path,"wb").write(b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",struct.pack(">IIBBBBB",n,n,8,2,0,0,0))+ch(b"IDAT",zlib.compress(raw,9))+ch(b"IEND",b""))
for n in (180,192,512): png(f"static/icon-{n}.png",n)
