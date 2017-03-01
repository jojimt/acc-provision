// Copyright 2017 Cisco Systems, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

type uniqueNameTest struct {
	components []string
	result     string
	desc       string
}

var uniqueNameTests = []uniqueNameTest{
	{[]string{}, "", "empty"},
	{[]string{"a", "b", "c"}, "a-b-c", "simple"},
	{[]string{"0", "1", "9"}, "0-1-9", "numbers"},
	{[]string{"AA", "BB", "ZZ"}, "AA-BB-ZZ", "caps"},
	{[]string{"a -", "-", "_"}, "a--20---2d----2d----5f-", "encode"},
}

func TestUniqueName(t *testing.T) {
	for _, at := range uniqueNameTests {
		assert.Equal(t, at.result,
			generateUniqueName(at.components...), at.desc)
	}
}

type indexDiffTest struct {
	ktype      string
	key        string
	objects    aciSlice
	expAdds    aciSlice
	expUpdates aciSlice
	expDeletes []string
	desc       string
}

func setDispName(displayName string, aci *Aci) *Aci {
	aci.Spec.SecurityGroup.DisplayName = displayName
	return aci
}

var indexDiffTests = []indexDiffTest{
	{"sec-group", "a", nil, nil, nil, nil, "empty"},
	{"sec-group", "a",
		aciSlice{NewSecurityGroup("common", "test")},
		aciSlice{NewSecurityGroup("common", "test")},
		nil, nil, "add"},
	{"sec-group", "a",
		aciSlice{setDispName("test", NewSecurityGroup("common", "test"))},
		nil,
		aciSlice{setDispName("test", NewSecurityGroup("common", "test"))},
		nil, "update"},
	{"sec-group", "a", nil, nil, nil,
		[]string{"test-common-SecurityGroup"}, "delete"},
	{"sec-group", "a",
		aciSlice{
			NewSecurityGroup("common", "test1"),
			NewSecurityGroup("common", "test2"),
			NewSecurityGroup("common", "test3"),
			NewSecurityGroup("common", "test4"),
		},
		aciSlice{
			NewSecurityGroup("common", "test1"),
			NewSecurityGroup("common", "test2"),
			NewSecurityGroup("common", "test3"),
			NewSecurityGroup("common", "test4"),
		},
		nil, nil, "addmultiple"},
	{"sec-group", "a",
		aciSlice{
			NewSecurityGroup("common", "test1"),
			NewSecurityGroup("common", "test4"),
			NewSecurityGroup("common", "test3"),
			NewSecurityGroup("common", "test2"),
		},
		nil, nil, nil, "nochange"},
	{"sec-group", "a",
		aciSlice{
			NewSecurityGroup("common", "test1"),
			NewSecurityGroup("common", "test0"),
			setDispName("test2", NewSecurityGroup("common", "test2")),
			NewSecurityGroup("common", "test3"),
			NewSecurityGroup("common", "test5"),
		},
		aciSlice{
			NewSecurityGroup("common", "test0"),
			NewSecurityGroup("common", "test5"),
		},
		aciSlice{
			setDispName("test2", NewSecurityGroup("common", "test2")),
		},
		[]string{"test4-common-SecurityGroup"},
		"mixed"},
	{"sec-group", "b",
		aciSlice{
			NewSecurityGroup("common", "septest"),
		},
		aciSlice{
			NewSecurityGroup("common", "septest"),
		},
		nil, nil, "diffkey"},
}

func TestAimIndexDiff(t *testing.T) {
	cont := testController()
	cont.run()

	for _, it := range indexDiffTests {
		cont.aimAdds = nil
		cont.aimUpdates = nil
		cont.aimDeletes = nil
		for _, o := range it.expAdds {
			addAimLabels(it.ktype, it.key, o)
		}
		for _, o := range it.expUpdates {
			addAimLabels(it.ktype, it.key, o)
		}

		cont.writeAimObjects(it.ktype, it.key, it.objects)
		assert.Equal(t, it.expAdds, cont.aimAdds, "adds", it.desc)
		assert.Equal(t, it.expUpdates, cont.aimUpdates, "updates", it.desc)
		assert.Equal(t, it.expDeletes, cont.aimDeletes, "deletes", it.desc)
	}

	cont.stop()
}

func TestAimFullSync(t *testing.T) {

	i := 0
	j := 1
	for j < len(indexDiffTests)-1 { // last test case doesn't apply to this
		cont := testController()

		it := &indexDiffTests[i]

		for _, o := range it.objects {
			addAimLabels(it.ktype, it.key, o)
			cont.fakeAimSource.Add(o)
		}
		cont.run()

		it = &indexDiffTests[j]
		cont.writeAimObjects(it.ktype, it.key, it.objects)
		cont.aimAdds = nil
		cont.aimUpdates = nil
		cont.aimDeletes = nil

		cont.aimFullSync()

		for _, o := range it.expAdds {
			addAimLabels(it.ktype, it.key, o)
		}
		for _, o := range it.expUpdates {
			addAimLabels(it.ktype, it.key, o)
		}
		assert.Equal(t, it.expAdds, cont.aimAdds, "adds", it.desc)
		assert.Equal(t, it.expUpdates, cont.aimUpdates, "updates", it.desc)
		assert.Equal(t, it.expDeletes, cont.aimDeletes, "deletes", it.desc)

		i++
		j++

		cont.stop()
	}

}