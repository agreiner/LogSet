#!/usr/bin/env python3

# next step is to refactor I think
# the handlers need to handle creating LogSeries
# and the remote-vs-local logfiles need to be handled with better separation-of-concerns
#   (is this something a visitor pattern is suitable for? no. template method might do it)

import sys
# system python 3 on Cori is broken so user will need to load a
# python module, which will be 3.6+ anyway, so we'll take advantage
# of some of python's modern features:
print(sys.version_info)
if sys.version_info[0] < 3 or sys.version_info[1] < 5:
    raise Exception("Requires python 3.5+, try module load python/3.6-anaconda-4.4")

sys.path.append('/global/homes/s/sleak/Monitoring/Resilience/LogSet/src')
import LogsGraph
import handlers
import rdflib

import re

# each new file in the index needs a unique iri:
import random, string
def ran_str(len):
    " produce a string of random letters, of a given length "
    return ''.join([random.choice(string.ascii_lowercase) for i in range(len)])


# support class for holding info about a given LogSeries:
class LogSeries:
    def __init__(self, name, uri):
        self.name = name
        self.uri = uri
        self.mediaType = None
        self.logFormatType = None
        self.fmtInfo = {}


# from https://stackoverflow.com/questions/2533120/show-default-value-for-editing-on-python-input-possible
# input() that writes text for user to edit
import readline
def rlinput(prompt, prefill=''):
   readline.set_startup_hook(lambda: readline.insert_text(prefill))
   try:
      return input(prompt)
   finally:
      readline.set_startup_hook()


if __name__ == '__main__':

    # concrete first tool: given a path and optionally some urls with
    # dictionaries etc to include, walk the path for files and guide the user 
    # through creating an index for this dataset (included an expanded 
    # dictionary if required)

    #import logging
    #logger = logging.getLogger()
    #logger.setLevel(logging.DEBUG)

    usage = sys.argv[0] + " <path> [<url> <url> ...]\n"
    usage += "eg:\n"
    sampledir = "/global/cscratch1/sd/sleak/Resilience/corismw-sample-p0/p0-20170906t151820"
    sampleurl = "file:///global/homes/s/sleak/Monitoring/Resilience/LogSet/examples"
    usage += sys.argv[0] + " {0} {1} {2}\n".format(sampledir, sampleurl+'/nersc.ttl', 
                                                   sampleurl+'/cray-dict.ttl')
    if len(sys.argv) <= 1:
        print(usage)
        sys.exit(2)
    path = sys.argv[1]

    # TODO get the baseuri from options
    baseuri = "cori.nersc.gov:{}/".format(sys.argv[1]) 

    graph = LogsGraph.construct(*sys.argv[2:], spider=True)

    # What filepatterns do we recognize?
    filepatterns = {} # tag: regex
    query = '''SELECT ?tag ?regex WHERE {
                ?id a dict:FilenamePattern .
                ?id dict:tag ?tag .
                ?id dict:regex ?regex .
            }'''
    for row in graph.query(query):
        filepatterns[str(row[0])] = str(row[1])
    #print(filepatterns)

    # and which LogSeries does each pattern correspond to?
    series = {} # regex: logseries_name
    query = '''select ?filepattern ?logseries where {
                ?logseries a logset:LogSeries .
                ?logseries logset:logFormatInfo ?filepattern .
                filter regex(?filepattern, "^filepattern=") .
            }'''
    # require tags to be alphanumeric: (but allow : to separate name from pattern)
    # FIXME allow ":" as well, to separate name:pattern
    tags = re.compile('(<[\w:]+>)')
    for row in graph.query(query):
        fp = str(row[0]).partition('=')[2]
        plist = tags.split(fp)
        # replace tags with filepatterns:
        plist2 = [ filepatterns[t] for t in plist[1::2] ]
        # merge them back into a string (which is the "expanded" filepattern):
        p = ''.join(sum(zip(plist[:-1:2],plist2),())) + plist[-1]
        #print(p)
        #series[re.compile(p)] = str(row[1]).rpartition('#')[2]
        series[p] = str(row[1]).rpartition('#')[2]

    # info about each series:
    series_info = {} # logseries_name: LogSeries
    query = '''select ?logseries ?logfmt ?mediatype where {
                ?logseries a logset:LogSeries .
                ?logseries logset:logFormat ?logfmt .
                optional { ?logfmt dcat:mediaType ?mediatype } .
            }'''
    for row in graph.query(query):
        series_name = str(row[0]).rpartition('#')[2]
        if series_name in series_info:
            raise Exception("same series ({0}) defined twice?".format(series_name))
        series_info[series_name] = LogSeries(series_name, row[0])
        series_info[series_name].logFormatType = str(row[1]).rpartition('#')[2]
        if len(row)>=3:
            series_info[series_name].mediaType = str(row[2])
# actually, *do* collect the filepattern, it is useful sometimes:
#    query = '''select ?logseries ?fmtinfo where {
#                ?logseries a logset:LogSeries .
#                ?logseries logset:logFormatInfo ?fmtinfo .
#                filter (!regex(?fmtinfo, "^filepattern=")) .
#            }'''
    query = '''select ?logseries ?fmtinfo where {
                ?logseries a logset:LogSeries .
                ?logseries logset:logFormatInfo ?fmtinfo .
            }'''
    for row in graph.query(query):
        series_name = str(row[0]).rpartition('#')[2]
        key, sep, value = str(row[1]).partition('=')
        series_info[series_name].fmtInfo[key] = value
        # collect the regexs associated with each filepattern:
        # (needed by filePerTimepoint). There might be multiple
        # regexes for a LogSeries so incorporate the tag in the key
        # hmm, filepattern might be more complex than a single tag..
        #if key == "filepattern":
        #    #series_info[series_name].fmtInfo["file_regex_{}".format(value)] = filepatterns[value]
        #    series_info[series_name].fmtInfo["file_regex_{}".format(value)] = series[value]

    # make a new graph for this index, with its own namespace 
    # TODO get the namespace from command-line options:
    ns = rdflib.Namespace("http://example.org/myindex#")
    newindex = rdflib.ConjunctiveGraph()
    newindex.namespace_manager.bind("myindex", ns)
    # namespace for dict with new series:
    mydict_ns = rdflib.Namespace("http://example.org/mydict#")
    mydict = None

    import os
    baselen = len(path)+1
    remaining = set([(base[baselen:],f) for base, d, files in os.walk(path) for f in files])
    indexed = set()
    todo = set(series.keys())  # todo is patterns still to look for
    todo.add("") # empty string will be processed last, ensured we look for files 
                 # if/after there are no more patterns to try
    print("{} files remaining".format(len(remaining)))

    # some nodes we will need when added files to the graph:
    #logset = LogsGraph.get("logset", "LogSet")
    #concretelog = LogsGraph.get("logset","ConcreteLog")
    #isinstanceof = LogsGraph.get("logset","isInstanceOf")
    #dcat =  rdflib.Namespace(LogsGraph.getns('dcat'))

    # some namespaces fthat we use when adding nodes to the graph:
    #dcat   = rdflib.Namespace(LogsGraph.getns('dcat'))
    #logset = rdflib.Namespace(LogsGraph.getns('logset'))
    dcat   = LogsGraph.getns('dcat')
    logset = LogsGraph.getns('logset')
    rdf = rdflib.namespace.RDF
    xsd = rdflib.namespace.XSD
    
    # add the index itself:
    newindex.add( (ns['index'], rdf.type, logset.LogSet) )
    # TODO: get and add all the other properties of the index itself
    # (each logfile adds itself as a distribution)

    # TODO .. and need to find a good way to enforce pattern format
    # of having an "outer" group that captures the whole tag
    while len(todo) > 0:
        for p in sorted(todo, key=len, reverse=True):
            if p=="":
                break
            print("indexing files matching {}".format(p))
            regex = re.compile(p)
            logseries = series_info[series[p]]
            matching = set(filter(lambda x: regex.match(x[1]), remaining))
            # this is so handlers can extract metadata from filepattern:
            logseries.fmtInfo['fileregex'] = regex 
            # now create an entry in the new graph for this file
            for m in matching:
                filepath = os.sep.join([path, m[0], m[1] ])
                try:
                    handler = handlers.factory(logseries.logFormatType, filepath, logseries.fmtInfo)
                except handlers.UnsupportedLogFormatHandler:
                    print("TODO handle new things! {}".format(logseries.logFormatType))
                t_start, t_end = handler.timespan()
                size = handler.size
                # TODO get confirmation from user before creating the entry and 
                # removing it from the 'remaining' set

                id = ran_str(8)
                newindex.add( (ns[id], rdf.type, logset.ConcreteLog) )
                newindex.add( (ns[id], dcat.downloadURL, rdflib.URIRef(baseuri + os.sep.join(i for i in m if i))) ) 
                newindex.add( (ns[id], logset.isInstanceOf, logseries.uri) )
                newindex.add( (ns[id], dcat.byteSize, rdflib.Literal(handler.size, datatype=xsd.integer)) )
                # temporal info needs a blank node:
                bnode = rdflib.BNode()
                newindex.add( (ns[id], dcat.temporal, bnode) )
                newindex.add( (bnode, logset.startDate, rdflib.Literal(t_start, datatype=xsd.integer)) )
                newindex.add( (bnode, logset.endDate, rdflib.Literal(t_end, datatype=xsd.integer)) )
                newindex.add( (ns[id], dcat.byteSize, rdflib.Literal(size, datatype=xsd.integer)) )
                # add file to index:
                newindex.add( (ns['index'], dcat['distribution'], ns[id]) )
            remaining -= matching
            print("{} files remaining".format(len(remaining)))
            indexed.add(p)
            todo.remove(p)
            print("{} patterns indexed".format(len(indexed)))
        print("TODO look for files not matching any pattern")
        # now find files whose pattern we don't know
        print(todo)
        for m in remaining:
            fpath = os.sep.join(i for i in m if i)
            print("found a new file pattern: {}\nDoes it correspond to any of these known LogSeries?".format(fpath))
            series_keys = (n for n in series_info.keys())
            print("{0:d}  {1:s}".format(0, '(none)'))
            #for i,name in zip(range(1,len(series_keys)+1),series_keys):
            for i,name in range(1,len(series_keys)+1):
                #print("{0:d}  {1:s}".format(i, name))
                print("{0:d}  {1:s}".format(i, series_keys[i]))
            nb = int(input('[0 - (none)]: ') or 0)
            # if it is a known series, add it to filepattern for that series, otherwise make a new series
            # first, try to guess the pattern:
            for tag in sorted(filepatterns.keys(), key=len, reverse=True):
                regex = re.compile(filepatterns[tag])
                match = regex.search(fpath)
                if match:
                    span = match.span() # TODO this relies on first group being "outer", enforce that
                    prefill='^{0}{1}{2}$'.format(fpath[:span[0]], tag, fpath[span[1]:])
                    pattern = rlinput(prompt="", prefill=prefill)
                    if len(pattern)>0:
                        break # we've found it
            else:
                # doesn't match any of our known tags, user will have to create one and corresponding regex
                # (might be an exact-match, not a pattern. Or an "ignore this file")
                raise Exception("creating new tags not yet implemented")
            # if a known series was selected, add this filepattern to it
            if nb == 0:
                # a new series:
                proposed_name = '{}_logfile'.format(fpath[:span[0]])
                series_name = rlinput("Name for this LogSeries? ", prefill=proposed_name)
                # TODO make sure series_name not already in use in mydict ns
                if mydict is None:
                    mydict = rdflib.ConjunctiveGraph()
                    mydict.namespace_manager.bind("mydict", mydict_ns)
                mydict.add( (mydict_ns[series_name], rdf.type, logset.LogSeries) )
                series_info[series_name] = LogSeries(series_name, mydict_ns[series_name])
                # TODO need to work out what type of file it is and set logFormat and logFormatInfo
                # and mediatype
                # also a comment is worthwhile
                # something like:
                # for h in handlers:
                #    h.test_if_this_file_matches_this_handler
                #mydict.add( (mydict_ns[series_name], logset.logFormat, 
            else:
                series_name = series_keys[nb]
            series_info[series_name].fmtInfo['filepattern']=pattern 

            # in either case, add the new pattern to the totdo list and break
            #actualpattern = ...
            #todo.add(actualpattern) # or something...
            break # out of remaining loop
        else:
            todo.remove("") # breaks out of  while loop

        # only remove the final "" from todo pattern list when no more patterns to be found
        #break

    out = newindex.serialize(format='n3').decode('ascii')
    #for line in out.splitlines()[:40]:
    #    print(line)
    #for line in out.splitlines()[-40:]:
    #    print(line)
#
#    import os
#    baselen = len(path)+1
#    count=1
#    for d,f in [(base[baselen:],file) for base, dir, files in os.walk(path) for file in files]:
#        print((d,f))
#        count+=1
#        if count>10: break
        # algorithm will be:
        # - compare the filename against each of the known patterns
        # ask the user which (or none) of the matching patterns this file corresponds to
        # (maybe print the first few lines of the file, if it is text, or the type otherwise
        # as a hint)
        # the user can select a logseries, or "something new" (then give info on new series),
        # or "skip file", "skip pattern", "ignore pattern (skip all such)"
        # if we have a new series to create, define it via a template, add it to the graph in
        # a "mydict" namespace
        # add file to index in a "newindex" namespace
        # call on appropriate handler class to guess info about the file (so handler class 
        # needs to register itself and support and "index(new_item)" call 
        
#### -- deprecated code ---        
#    tagpattern = re.compile('<\w+>')
#    for row in graph.query(query):
#        filepattern = str(row[0]).partition('=')[2]
#        # filepattern may contain tags to convert to regexs: 
#        for tag in tagpattern.findall(filepattern):
#            filepattern = filepattern.replace(tag, patterns[tag])
#        regex = re.compile(filepattern)
#        series = row[1]
#        logseries[filepattern] = (regex, series)
#    print(sorted(logseries, key=len, reverse=True))
#
#    logfmts = {} # series: handler_label
#    fmtinfo = {} # series: {key, value}
#    query = '''SELECT ?logseries ?logfmt ?mediatype WHERE {
#                    ?logseries a logset:LogSeries .
#                    ?logseries logset:logFormatInfo ?filepattern .
#                    ?logseries logset:logFormat ?logfmt .
#                    OPTIONAL { ?logfmt dcat:mediaType ?mediatype } .
#                    FILTER regex(?filepattern, "^filepattern=") .
#            }'''

#    logseries = {} # filepattern: (regex, logseries, logformat, mediatype)
#    query = '''SELECT ?filepattern ?logseries ?logfmt ?mediatype WHERE {
#                    ?logseries a logset:LogSeries .
#                    ?logseries logset:logFormatInfo ?filepattern .
#                    ?logseries logset:logFormat ?logfmt .
#                    OPTIONAL { ?logfmt dcat:mediaType ?mediatype } .
#                    FILTER regex(?filepattern, "^filepattern=") .
#            }'''
#    tagpattern = re.compile('<\w+>')
#    for row in graph.query(query):
#        filepattern = str(row[0]).partition('=')[2]
#        # filepattern may contain tags to convert to regexs: 
#        for tag in tagpattern.findall(filepattern):
#            filepattern = filepattern.replace(tag, patterns[tag])
#            print (tag)
#            print (patterns[tag])
#            print (filepattern)
#        regex = re.compile(filepattern)
#        series = row[1]
#        logfmt = row[2]
#        mediatype = str(row[3])
#        print("got filepattern: {}".format(filepattern))
#        print("got regex: {}".format(regex))
#        #print("got logseries: {}".format(series))
#        #print("got logfmt: {}".format(logfmt))
#        #print("got mediatype: {}".format(mediatype))
#        logseries[filepattern] = (regex, series, logfmt, mediatype) 
#    print(sorted(logseries, key=len, reverse=True))
#
#
#
#
#    import os
#    baselen = len(path)+1
#    remaining = set([(base[baselen:],f) for base, d, files in os.walk(path) for f in files])
#    indexed = set()
#    todo = set(logseries.keys())
#    todo.add("") # empty string will be processed last, ensured we look for files 
#                 # if/after there are no more patterns to try
#    print("{} files remaining".format(len(remaining)))
#
#    # some nodes we will need when added files to the graph:
#    logset = LogsGraph.get("logset", "LogSet")
#    concretelog = LogsGraph.get("logset","ConcreteLog")
#    isinstanceof = LogsGraph.get("logset","isInstanceOf")
#    dcat =  rdflib.Namespace(LogsGraph.getns('dcat'))
#    
#    # add the index itself:
#    newindex.add( (ns['index'], rdflib.RDF.type, logset) )
#    # TODO: get and add all the other properties of the index itself
#    # (each logfile adds itself as a distribution)
#
#    while len(todo) > 0:
#        for p in sorted(todo, key=len, reverse=True):
#            if p=="":
#                break
#            print("indexing files matching {}".format(p))
#            regex = logseries[p][0]
#            series = logseries[p][1]
#            logfmt = logseries[p][2]
#            matching = set(filter(lambda x: regex.match(x[1]), remaining))
#            # should get confirmation from user before removing from remaining
#            # now create an entry in the new graph for this file
#            for m in matching:
#                id = ran_str(8)
#                newindex.add( (ns[id], rdflib.RDF.type, concretelog) )
#                #print( dcat['downloadURL'])
#                #print(m)
#                #print(rdflib.URIRef(baseuri + m))
#                newindex.add( (ns[id], dcat['downloadURL'], rdflib.URIRef(baseuri + m[-1])) )
#                newindex.add( (ns[id], isinstanceof, series) )
#                # TODO: have to get the temporal info
#                # need to instantiate appropriate LogFormatType
#                #handler = LogFormatType.factory(logfmt)
#                # add file to index:
#                newindex.add( (ns['index'], dcat['distribution'], ns[id]) )
#
#            remaining -= matching
#            print("{} files remaining".format(len(remaining)))
#            indexed.add(p)
#            todo.remove(p)
#            print("{} patterns indexed".format(len(indexed)))
#        print("TODO looks for files not matching any pattern")
#        break
#
#    out = newindex.serialize(format='n3').decode('ascii')
#    for line in out.splitlines()[:40]:
#        print(line)
#    for line in out.splitlines()[-40:]:
#        print(line)
##
##
##
##
##
##
